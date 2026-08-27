#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def update_manifest(manifest: Path, raw_files: list[list[str]]) -> None:
    replacements = {
        iso_path: md5(Path(raw_path).resolve())
        for iso_path, raw_path in raw_files
    }
    counts = {iso_path: 0 for iso_path in replacements}
    output: list[str] = []

    for line in manifest.read_text().splitlines():
        fields = line.split(maxsplit=1)

        if len(fields) != 2 or fields[1] not in replacements:
            output.append(line)
            continue

        iso_path = fields[1]
        output.append(f"{replacements[iso_path]}  {iso_path}")
        counts[iso_path] += 1

    invalid = {path: count for path, count in counts.items() if count != 1}

    if invalid:
        raise RuntimeError(f"Expected one MD5 manifest entry per path, got {invalid}")

    manifest.write_text("\n".join(output) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update selected files in an ISO-style md5sum.txt manifest"
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--file",
        nargs=2,
        action="append",
        required=True,
        metavar=("ISO_MANIFEST_PATH", "LOCAL_FILE"),
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    manifest = arguments.manifest.resolve()

    if not manifest.is_file():
        raise FileNotFoundError(manifest)

    update_manifest(manifest, arguments.file)
    print(f"Updated {len(arguments.file)} entries in {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
