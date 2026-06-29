#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         init.py
#  Purpose:      Create .env and per-service env/<service>.env files from templates
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Bootstrap local config files from their templates.

Copies .env.example to .env and each env.example/<service>.env to env/<service>.env. Existing files are left untouched (reported as skipped) so the target is safe to re-run. Errors go to stderr with a non-zero exit.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from _common import (
    DOTENV_FILE,
    DOTENV_TEMPLATE,
    ENV_DIR,
    ENV_TEMPLATE_DIR,
    _print,
    _rel_path,
)


def _copy_if_absent(*, dst_path: Path, src_path: Path) -> None:
    """Copy src_path to dst_path unless dst_path already exists - report the outcome."""
    if dst_path.exists():
        _print(header=_rel_path(path=dst_path), msg="Skipped (already exists)")
        return
    shutil.copy(dst=dst_path, src=src_path)
    _print(header=_rel_path(path=dst_path), msg="Created")


def main() -> int:
    if not (DOTENV_TEMPLATE.exists()):
        _print(
            header=_rel_path(path=DOTENV_TEMPLATE),
            msg="Missing template",
        )
        return 1
    _copy_if_absent(dst_path=DOTENV_FILE, src_path=DOTENV_TEMPLATE)

    ENV_DIR.mkdir(exist_ok=True, parents=True)

    templates = sorted(ENV_TEMPLATE_DIR.glob("*.env"))
    if not (templates):
        template_dir = f"{_rel_path(path=ENV_TEMPLATE_DIR)}/"
        _print(
            header=template_dir,
            msg="No component *.env templates",
        )
        return 1

    for src in templates:
        _copy_if_absent(dst_path=ENV_DIR / src.name, src_path=src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
