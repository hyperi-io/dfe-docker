#  Project:      dfe-docker
#  File:         _common.py
#  Purpose:      Single source of truth for the repo resources.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Shared repo layout for the scripts/tooling.

Internal support module - imported by the runnable scripts, not executed directly. Resolves the repo root once and exposes the well-known file/dir locations the scripts read and write so every script agrees on where things live. Add further shared locations here, alphabetical by name.
"""

from __future__ import annotations

import sys
import typing
from pathlib import Path

# File to mark the root of the repository
_REPO_MARKER = "docker-compose.yml"


def __find_repo_root(*, marker: str = _REPO_MARKER) -> Path:
    """Return the nearest ancestor (inclusive) of this module containing marker."""
    current = Path(__file__).resolve()
    for candidate in (current, *current.parents):
        if (candidate / marker).exists():
            return candidate
    raise SystemExit(f"Could not locate repo root (no {marker!r} in any parent)")


# Basic constants
FALSY = {"", "0", "false", "no", "off"}

# Repo constants
REPO_ROOT = __find_repo_root()
CONFIG_DIR = REPO_ROOT / "config"
DOTENV_FILE = REPO_ROOT / ".env"
DOTENV_TEMPLATE = REPO_ROOT / ".env.example"
ENV_DIR = REPO_ROOT / "env"
ENV_TEMPLATE_DIR = REPO_ROOT / "env.example"
PROFILE_MK = REPO_ROOT / ".profile.mk"
RUST_BUILDER = REPO_ROOT / "docker" / "dfe-rust-builder.Dockerfile"
SERVICE_PROFILES_FILE = REPO_ROOT / "service_profiles.yaml"


def _print(
    *, file: typing.TextIO | None = sys.stderr, header: str | None = None, msg: str
) -> None:
    """Print a custom message with caller script name prefixed."""
    print(
        f"{Path(sys.argv[0]).stem}{f' ({header})' if header else ''}: {msg}", file=file
    )


def _rel_path(*, path: Path) -> str:
    """Return path relative to the repo root for display."""
    return str(path.relative_to(REPO_ROOT))
