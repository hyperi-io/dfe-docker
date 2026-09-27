#  Project:      dfe-docker
#  File:         tests/conftest.py
#  Purpose:      Put scripts/ on sys.path and give each test its own .env
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Fixtures for the helper-script tests.

The scripts are flat modules rather than a package (`import _common`), so the
directory holding them goes on sys.path before anything imports them.

Each module binds the repo's real `.env` into its own global at import time, so
the `dotenv` fixture repoints every one of those globals at a temporary file. A
module missed here would read, and in dev_posture's and post's case rewrite, the
developer's own credentials.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _common  # noqa: E402
import check_compose  # noqa: E402
import creds  # noqa: E402
import dev_posture  # noqa: E402
import init  # noqa: E402
import post  # noqa: E402


@pytest.fixture
def dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return the path of a throwaway .env every module under test points at."""
    path = tmp_path / ".env"
    for module in (_common, check_compose, creds, dev_posture, init, post):
        monkeypatch.setattr(module, "DOTENV_FILE", path)
    # The access summary carries plaintext passwords, so it goes to tmp_path too.
    for module in (creds, post):
        monkeypatch.setattr(
            module, "ACCESS_SUMMARY_FILE", tmp_path / "access-summary.md"
        )
    # _rel_path resolves REPO_ROOT at call time, so display paths stay renderable.
    monkeypatch.setattr(_common, "REPO_ROOT", tmp_path)
    return path
