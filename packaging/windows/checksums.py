"""
packaging/windows/checksums.py

Writes SHA256SUMS.txt beside the release files it is given, one line each in
the usual `sha256sum` format (lowercase hex, two spaces, the bare filename):

    python packaging/windows/checksums.py dist/installer/ScheduleMaxing-Setup-1.2.3.exe

The updater (app/update) downloads this file with the installer and refuses
an installer whose digest is not listed in it. `--verify` checks files
against an existing SHA256SUMS.txt instead. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

FILENAME = "SHA256SUMS.txt"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(files: list[Path], target: Path | None = None) -> Path:
    target = target or files[0].parent / FILENAME
    lines = [f"{sha256_of(path)}  {path.name}" for path in sorted(files, key=lambda path: path.name)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return target


def parse_checksums(text: str) -> dict[str, str]:
    """{filename: lowercase hex digest}; lines that are not `<64 hex>  <name>` are ignored."""
    entries = {}
    for line in text.splitlines():
        digest, _, name = line.strip().partition(" ")
        name = name.strip().lstrip("*")
        if len(digest) == 64 and all(character in "0123456789abcdefABCDEF" for character in digest) and name:
            entries[name] = digest.lower()
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or verify SHA256SUMS.txt for release files.")
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if args.verify:
        listed = parse_checksums((args.files[0].parent / FILENAME).read_text(encoding="utf-8"))
        bad = [path.name for path in args.files if listed.get(path.name) != sha256_of(path)]
        for name in bad:
            print(f"MISMATCH  {name}")
        return 1 if bad else 0
    target = write_checksums(args.files)
    print(target.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
