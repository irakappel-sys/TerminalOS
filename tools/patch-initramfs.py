#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bz2
import ctypes
import ctypes.util
import gzip
import lzma
import os
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path


CPIO_MAGICS = (b"070701", b"070702")
HEADER_SIZE = 110
TARGETS = {
    ".random-seed",
    "var/lib/systemd/random-seed",
    "var/lib/urandom/random-seed",
}


def align4(value: int) -> int:
    return (value + 3) & ~3


@dataclass(frozen=True)
class CpioEntry:
    name: str
    start: int
    end: int


def parse_cpio_entry(archive: bytes, offset: int) -> CpioEntry:
    header = archive[offset : offset + HEADER_SIZE]

    if len(header) != HEADER_SIZE or header[:6] not in CPIO_MAGICS:
        raise ValueError(f"Invalid newc CPIO header at offset {offset}")

    try:
        file_size = int(header[54:62], 16)
        name_size = int(header[94:102], 16)
    except ValueError as error:
        raise ValueError(f"Malformed newc CPIO header at offset {offset}") from error

    name_start = offset + HEADER_SIZE
    name_end = name_start + name_size

    if name_size < 1 or name_end > len(archive):
        raise ValueError(f"Invalid CPIO filename at offset {offset}")

    raw_name = archive[name_start : name_end - 1]
    name = raw_name.decode("utf-8", errors="surrogateescape")
    data_start = align4(name_end)
    entry_end = align4(data_start + file_size)

    if entry_end > len(archive):
        raise ValueError(f"Truncated CPIO entry {name!r}")

    return CpioEntry(name=name, start=offset, end=entry_end)


def normalize_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]

    return name.lstrip("/")


def cpio_archive_end(archive: bytes, start: int = 0) -> int:
    offset = start

    while True:
        entry = parse_cpio_entry(archive, offset)
        offset = entry.end

        if entry.name == "TRAILER!!!":
            return offset


def patch_cpio_archive(archive: bytes) -> tuple[bytes, list[str]]:
    output = bytearray()
    removed: list[str] = []
    offset = 0

    while True:
        entry = parse_cpio_entry(archive, offset)
        normalized = normalize_name(entry.name)

        if normalized in TARGETS:
            removed.append(normalized)
        else:
            output.extend(archive[entry.start : entry.end])

        offset = entry.end

        if entry.name == "TRAILER!!!":
            output.extend(archive[offset:])
            break

    return bytes(output), removed


def split_early_archives(initramfs: bytes) -> tuple[list[bytes], bytes]:
    archives: list[bytes] = []
    offset = 0

    while initramfs[offset : offset + 6] in CPIO_MAGICS:
        end = cpio_archive_end(initramfs, offset)

        while end < len(initramfs) and initramfs[end] == 0:
            end += 1

        archives.append(initramfs[offset:end])
        offset = end

    if offset == len(initramfs):
        raise ValueError("Initramfs has no compressed main archive")

    return archives, initramfs[offset:]


def decompress_main(data: bytes) -> tuple[str, bytes, bytes]:
    if data.startswith(b"\xfd7zXZ\x00"):
        decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        content = decompressor.decompress(data)
        return "xz", content, decompressor.unused_data

    if data.startswith(b"\x1f\x8b"):
        stream = zlib.decompressobj(wbits=31)
        content = stream.decompress(data) + stream.flush()
        return "gzip", content, stream.unused_data

    if data.startswith(b"BZh"):
        decompressor = bz2.BZ2Decompressor()
        content = decompressor.decompress(data)
        return "bzip2", content, decompressor.unused_data

    if data.startswith(b"\x28\xb5\x2f\xfd"):
        library_name = ctypes.util.find_library("zstd")

        if library_name is None:
            raise RuntimeError("Unable to locate libzstd")

        library = ctypes.CDLL(library_name)
        find_frame_size = library.ZSTD_findFrameCompressedSize
        find_frame_size.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        find_frame_size.restype = ctypes.c_size_t
        is_error = library.ZSTD_isError
        is_error.argtypes = (ctypes.c_size_t,)
        is_error.restype = ctypes.c_uint
        input_buffer = ctypes.c_char_p(data)
        frame_size = find_frame_size(input_buffer, len(data))

        if is_error(frame_size):
            raise RuntimeError("Unable to resolve the first Zstandard frame size")

        result = subprocess.run(
            ["zstd", "--quiet", "--decompress", "--stdout"],
            input=data[:frame_size],
            stdout=subprocess.PIPE,
            check=True,
        )
        return "zstd", result.stdout, data[frame_size:]

    raise ValueError("Unsupported initramfs compression format")


def compress_main(kind: str, content: bytes) -> bytes:
    if kind == "xz":
        return lzma.compress(
            content,
            format=lzma.FORMAT_XZ,
            check=lzma.CHECK_CRC32,
            preset=6,
        )

    if kind == "gzip":
        return gzip.compress(content, compresslevel=9, mtime=0)

    if kind == "bzip2":
        return bz2.compress(content, compresslevel=9)

    if kind == "zstd":
        result = subprocess.run(
            ["zstd", "--quiet", "-19", "--stdout"],
            input=content,
            stdout=subprocess.PIPE,
            check=True,
        )
        return result.stdout

    raise ValueError(f"Unsupported compression kind: {kind}")


def add_zero_padding(data: bytes, target_size: int) -> bytes:
    padding_size = target_size - len(data)

    if padding_size < 0:
        raise RuntimeError("Cannot pad data that already exceeds its target size")

    return data + bytes(padding_size)


def patch_initramfs(
    path: Path,
    preserve_size: bool,
    unpadded_copy: Path | None,
    allow_absent: bool,
) -> None:
    original = path.read_bytes()
    early_archives, compressed_main = split_early_archives(original)
    output = bytearray()
    removed: list[str] = []

    for archive in early_archives:
        patched, archive_removed = patch_cpio_archive(archive)
        output.extend(patched)
        removed.extend(archive_removed)

    compression, main, trailing = decompress_main(compressed_main)

    if compression == "zstd" and any(trailing):
        raise RuntimeError("Non-zero data follows the original Zstandard frame")

    patched_main, main_removed = patch_cpio_archive(main)
    removed.extend(main_removed)

    if not removed and not allow_absent:
        raise RuntimeError("No public random seed was found in the initramfs")

    if not removed:
        if any(trailing):
            raise RuntimeError("Non-zero data follows the clean compressed CPIO archive")

        if unpadded_copy is not None:
            if unpadded_copy.resolve() == path:
                raise ValueError("Unpadded verification copy must differ from the initramfs")

            unpadded_copy.parent.mkdir(parents=True, exist_ok=True)
            unpadded_size = len(original) - len(trailing)
            unpadded_copy.write_bytes(original[:unpadded_size])

        print(
            f"Verified that {path} already contains no public random seed "
            f"({compression}, {len(original)} bytes)"
        )
        return

    output.extend(compress_main(compression, patched_main))
    unpadded_output = bytes(output)
    output.extend(trailing)

    if unpadded_copy is not None:
        if unpadded_copy.resolve() == path:
            raise ValueError("Unpadded verification copy must differ from the initramfs")

        unpadded_copy.parent.mkdir(parents=True, exist_ok=True)
        unpadded_copy.write_bytes(unpadded_output)

    if preserve_size:
        if len(output) > len(original):
            raise RuntimeError(
                "Patched initramfs is larger than its original ISO extent: "
                f"{len(output)} > {len(original)} bytes"
            )

        if len(output) < len(original):
            if compression != "zstd" or any(trailing):
                raise RuntimeError(
                    "Exact-size padding is only supported after a final Zstandard "
                    "stream with no non-zero trailing data"
                )

            # Linux's initramfs unpacker explicitly skips NUL bytes between or after
            # archives.  A Zstandard skippable frame is not safe here: the kernel
            # consumes one frame at a time, then would treat the skippable-frame
            # magic as an unsupported compression format.
            output = bytearray(add_zero_padding(bytes(output), len(original)))

    mode = path.stat().st_mode & 0o7777
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_bytes(output)
    os.chmod(temporary, mode)
    temporary.replace(path)

    verify_early, verify_compressed = split_early_archives(path.read_bytes())

    for archive in verify_early:
        _, remaining = patch_cpio_archive(archive)

        if remaining:
            raise RuntimeError("Random seed remained in an early CPIO archive")

    _, verify_main, verify_trailing = decompress_main(verify_compressed)
    _, remaining = patch_cpio_archive(verify_main)

    if remaining:
        raise RuntimeError("Random seed remained in the main CPIO archive")

    if any(verify_trailing):
        raise RuntimeError("Non-zero data follows the final compressed CPIO archive")

    print(
        f"Removed {len(removed)} public random-seed entr"
        f"{'y' if len(removed) == 1 else 'ies'} from {path} ({compression}, "
        f"{len(output)} bytes)"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove fixed public random seeds from a concatenated initramfs"
    )
    parser.add_argument("initramfs", type=Path)
    parser.add_argument(
        "--preserve-size",
        action="store_true",
        help="pad a smaller final Zstandard stream with kernel-safe NUL bytes",
    )
    parser.add_argument(
        "--unpadded-copy",
        type=Path,
        help="also write the valid archive without exact-size trailing padding",
    )
    parser.add_argument(
        "--allow-absent",
        action="store_true",
        help="succeed when an already-repaired initramfs contains no public seed",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    path = arguments.initramfs.resolve()

    if not path.is_file():
        raise FileNotFoundError(path)

    patch_initramfs(
        path,
        preserve_size=arguments.preserve_size,
        unpadded_copy=(
            arguments.unpadded_copy.resolve()
            if arguments.unpadded_copy is not None
            else None
        ),
        allow_absent=arguments.allow_absent,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
