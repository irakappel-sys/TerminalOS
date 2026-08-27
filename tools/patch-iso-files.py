#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


SECTOR_SIZE = 2048
REPORT_PATTERN = re.compile(
    r"^File data lba:\s+\d+\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']+)'$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Replacement:
    iso_path: str
    source: Path
    lba: int
    blocks: int
    size: int

    @property
    def start(self) -> int:
        return self.lba * SECTOR_SIZE

    @property
    def end(self) -> int:
        return self.start + self.size


def report_extent(iso: Path, iso_path: str) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "xorriso",
            "-indev",
            str(iso),
            "-find",
            iso_path,
            "-exec",
            "report_lba",
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    matches = REPORT_PATTERN.findall(result.stdout)

    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one contiguous ISO extent for {iso_path}, found {len(matches)}"
        )

    raw_lba, raw_blocks, raw_size, reported_path = matches[0]

    if reported_path != iso_path:
        raise RuntimeError(
            f"xorriso reported {reported_path!r} while resolving {iso_path!r}"
        )

    return int(raw_lba), int(raw_blocks), int(raw_size)


def resolve_replacements(
    iso: Path, raw_replacements: list[list[str]]
) -> list[Replacement]:
    replacements: list[Replacement] = []

    for iso_path, raw_source in raw_replacements:
        if not iso_path.startswith("/"):
            raise ValueError(f"ISO path must be absolute: {iso_path}")

        source = Path(raw_source).resolve()

        if not source.is_file():
            raise FileNotFoundError(source)

        lba, blocks, size = report_extent(iso, iso_path)
        source_size = source.stat().st_size

        if source_size != size:
            raise RuntimeError(
                f"Replacement for {iso_path} must be exactly {size} bytes, "
                f"got {source_size}"
            )

        if size > blocks * SECTOR_SIZE:
            raise RuntimeError(f"Invalid extent allocation for {iso_path}")

        replacements.append(
            Replacement(
                iso_path=iso_path,
                source=source,
                lba=lba,
                blocks=blocks,
                size=size,
            )
        )

    replacements.sort(key=lambda item: item.start)

    for first, second in zip(replacements, replacements[1:]):
        if first.end > second.start:
            raise RuntimeError(
                f"Replacement extents overlap: {first.iso_path} and {second.iso_path}"
            )

    return replacements


def patch_iso(original: Path, output: Path, replacements: list[Replacement]) -> None:
    if original == output:
        raise ValueError("Output ISO must differ from the original ISO")

    if output.exists():
        raise FileExistsError(output)

    shutil.copyfile(original, output)

    with output.open("r+b") as iso_handle:
        for replacement in replacements:
            iso_handle.seek(replacement.start)

            with replacement.source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, iso_handle, length=8 * 1024 * 1024)

        iso_handle.flush()
        os.fsync(iso_handle.fileno())


def verify_only_allowed_bytes_changed(
    original: Path, output: Path, replacements: list[Replacement]
) -> None:
    original_size = original.stat().st_size

    if output.stat().st_size != original_size:
        raise RuntimeError("Patched ISO size changed")

    spans = [(item.start, item.end) for item in replacements]
    chunk_size = 8 * 1024 * 1024
    offset = 0
    changed = False

    with original.open("rb") as original_handle, output.open("rb") as output_handle:
        while offset < original_size:
            original_chunk = bytearray(original_handle.read(chunk_size))
            output_chunk = bytearray(output_handle.read(chunk_size))

            if len(original_chunk) != len(output_chunk):
                raise RuntimeError("ISO comparison encountered unequal chunk sizes")

            chunk_end = offset + len(original_chunk)

            if original_chunk != output_chunk:
                changed = True

            for span_start, span_end in spans:
                overlap_start = max(offset, span_start)
                overlap_end = min(chunk_end, span_end)

                if overlap_start >= overlap_end:
                    continue

                relative_start = overlap_start - offset
                relative_end = overlap_end - offset
                original_chunk[relative_start:relative_end] = bytes(
                    relative_end - relative_start
                )
                output_chunk[relative_start:relative_end] = bytes(
                    relative_end - relative_start
                )

            if original_chunk != output_chunk:
                raise RuntimeError(
                    f"ISO changed outside approved file extents near byte {offset}"
                )

            offset = chunk_end

    if not changed:
        raise RuntimeError("Patched ISO is byte-for-byte identical to the original")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replace fixed-size files inside an ISO without rewriting its boot records, "
            "partition tables, catalogs, or filesystem metadata"
        )
    )
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--replace",
        nargs=2,
        action="append",
        required=True,
        metavar=("ISO_PATH", "LOCAL_FILE"),
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    original = arguments.original.resolve()
    output = arguments.output.resolve()

    if not original.is_file():
        raise FileNotFoundError(original)

    replacements = resolve_replacements(original, arguments.replace)
    patch_iso(original, output, replacements)
    verify_only_allowed_bytes_changed(original, output, replacements)

    for replacement in replacements:
        print(
            f"Patched {replacement.iso_path}: bytes {replacement.start}-"
            f"{replacement.end - 1} ({replacement.size} bytes)"
        )

    print("All bytes outside the approved ISO file extents are unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
