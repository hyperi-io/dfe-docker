#  Project:      dfe-docker
#  File:         tests/conftest.py
#  Purpose:      Put scripts/ on sys.path and keep every test's writes out of the checkout
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

`chmod_watch` sees each chmod through an audit hook, which fires before the call,
so a test can prove a secret never sat in a file wider than its mode -- a check of
the final mode passes a write followed by a chmod, whatever was readable between.
"""

import contextlib
import os
import stat
import sys
from collections.abc import Iterator
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
import instances  # noqa: E402
import post  # noqa: E402
import resolve_profile  # noqa: E402


class ChmodWatch:
    """Every chmod a test makes, as the mode and text its target had just before it."""

    def __init__(self) -> None:
        self.views: list[tuple[int, str | None]] = []

    def exposed(self, *, secret: str, allowed: int) -> list[str]:
        """Return the modes at which `secret` sat in a file readable beyond `allowed`."""
        wider = 0o444 & ~allowed
        return [
            oct(mode)
            for mode, text in self.views
            if mode & wider and (text is None or secret in text)
        ]


_watch: ChmodWatch | None = None


def _on_audit(event: str, args: tuple) -> None:
    if _watch is None or event != "os.chmod":
        return
    with contextlib.suppress(OSError, TypeError, ValueError):
        target = args[0]
        info = os.stat(target)
        text: str | None = ""
        if info.st_size and isinstance(target, int):
            # A descriptor opened write-only cannot be read back; its content counts.
            text = None
        elif info.st_size:
            text = Path(target).read_text(encoding="utf-8", errors="replace")
        _watch.views.append((stat.S_IMODE(info.st_mode), text))


sys.addaudithook(_on_audit)


@pytest.fixture
def chmod_watch() -> Iterator[ChmodWatch]:
    """Record every chmod the test makes until it ends."""
    global _watch
    _watch = ChmodWatch()
    try:
        yield _watch
    finally:
        _watch = None


@pytest.fixture(autouse=True)
def generated_files_stay_in_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the files `make` generates at tmp_path, so no test rewrites the checkout's copies."""
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", tmp_path / ".profile.mk")
    monkeypatch.setattr(
        instances, "COMPOSE_INSTANCES_FILE", tmp_path / "docker-compose.instances.yml"
    )


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
