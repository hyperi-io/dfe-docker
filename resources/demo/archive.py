#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resources/demo/archive.py
#  Purpose:      Count, show, prune and purge the files dfe-archiver writes for the demo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Read and clear the archive volume for `make demo`.

dfe-archiver files each source's lines under <root>/<source>_land/ and has no retention of its own. `make demo` runs this in a throwaway Python container with the volume at /a: `prune` runs in a loop beside the stack so the archive stays small, `count` and `show` back the demo's PASS check, and `purge` empties a source's directory when the demo is removed.

Standard library only.
"""

import argparse
import sys
import time
from pathlib import Path

LAND_SUFFIX = "_land"
SAMPLE_CHARS = 200


def objects(*, root: Path, sources: list[str]) -> list[Path]:
    """Every file under root, or under the named sources' directories, oldest first."""
    bases = [root / f"{name}{LAND_SUFFIX}" for name in sources] if sources else [root]
    found = [
        path
        for base in bases
        if base.is_dir()
        for path in base.rglob("*")
        if path.is_file()
    ]
    return sorted(found, key=lambda path: path.stat().st_mtime)


def prune(*, max_age_seconds: float, max_bytes: int, now: float, root: Path) -> int:
    """Delete files older than the age, then the oldest until the rest fit the cap, and return how many went."""
    removed = 0
    for path in objects(root=root, sources=[]):
        if now - path.stat().st_mtime > max_age_seconds:
            path.unlink()
            removed += 1
    kept = objects(root=root, sources=[])
    total = sum(path.stat().st_size for path in kept)
    for path in kept:
        if total <= max_bytes:
            break
        total -= path.stat().st_size
        path.unlink()
        removed += 1
    # A directory the archiver rolls into again is recreated, so emptied ones can go
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()), reverse=True
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    return removed


def purge(*, root: Path, sources: list[str]) -> int:
    """Delete everything the named sources' directories hold, and the directories, and return how many files went."""
    removed = 0
    for name in sources:
        base = root / f"{name}{LAND_SUFFIX}"
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
                removed += 1
            else:
                path.rmdir()
        base.rmdir()
    return removed


def sample(*, path: Path) -> str:
    """The first line of an archived object, decompressed when it is zstd, cut short."""
    try:
        if path.suffix == ".zst":
            from compression import zstd

            with zstd.open(path, "rt", encoding="utf-8") as handle:
                line = handle.readline()
        else:
            with path.open(encoding="utf-8", errors="replace") as handle:
                line = handle.readline()
    except (ImportError, OSError, ValueError) as error:
        return f"(unreadable: {error})"
    line = line.strip()
    return line if len(line) <= SAMPLE_CHARS else f"{line[:SAMPLE_CHARS]}..."


def show(*, limit: int, root: Path, sources: list[str]) -> list[str]:
    """Lines naming the newest few objects with their size, then a sample of the newest."""
    found = objects(root=root, sources=sources)
    lines = [
        f"{path.relative_to(root)}  {path.stat().st_size} bytes"
        for path in found[-limit:]
    ]
    if found:
        lines.append(f"first line of the newest: {sample(path=found[-1])}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=("count", "show", "prune", "purge"))
    parser.add_argument("--root", default="/a", type=Path)
    parser.add_argument("--sources", default="", help="comma-separated source names")
    parser.add_argument("--limit", default=3, type=int)
    parser.add_argument("--max-age-minutes", default=15.0, type=float)
    parser.add_argument("--max-megabytes", default=200.0, type=float)
    parser.add_argument("--interval-minutes", default=0.0, type=float)
    args = parser.parse_args(argv)
    sources = [name for name in args.sources.split(",") if name]
    if args.action == "count":
        print(len(objects(root=args.root, sources=sources)))
    elif args.action == "show":
        print("\n".join(show(limit=args.limit, root=args.root, sources=sources)))
    elif args.action == "purge":
        print(purge(root=args.root, sources=sources))
    else:
        # With an interval this is the demo's pruner, which runs until its container is removed
        while True:
            removed = prune(
                max_age_seconds=args.max_age_minutes * 60,
                max_bytes=int(args.max_megabytes * 1024 * 1024),
                now=time.time(),
                root=args.root,
            )
            print(f"pruned {removed} file(s)", flush=True)
            if args.interval_minutes <= 0:
                break
            time.sleep(args.interval_minutes * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
