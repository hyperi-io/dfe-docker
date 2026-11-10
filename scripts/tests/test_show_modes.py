#  Project:      dfe-docker
#  File:         tests/test_show_modes.py
#  Purpose:      Assert `make modes` names how this checkout stays current
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`make modes` reads the mode from local files alone."""

from pathlib import Path
from typing import TypedDict

import pytest

import show_modes


class CurrentModeCase(TypedDict):
    """The daemon's state, what .env pins and the mode reported."""

    applied: str
    env_pin: str
    expected: str


CURRENT_MODE_CASES = [
    CurrentModeCase(
        applied="2.2.1+images.1a2b3c4d5e6f",
        env_pin="latest",
        expected="TRACK-LATEST (newest images)",
    ),
    CurrentModeCase(
        applied="2.2.1+images.1a2b3c4d5e6f", env_pin="2.2.1", expected="PINNED"
    ),
    CurrentModeCase(applied="", env_pin="latest", expected="LATEST (unpinned)"),
    CurrentModeCase(applied="", env_pin="", expected="UNCONFIGURED"),
]


class TestCurrentMode:
    @pytest.mark.parametrize("case", CURRENT_MODE_CASES)
    def test_matches_expected(
        self,
        dotenv: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        case: CurrentModeCase,
    ) -> None:
        state = tmp_path / ".dfe-stack-applied"
        if case["applied"]:
            state.write_text(f"{case['applied']}\n", encoding="utf-8")
        dotenv.write_text(f"DFE_STACK_VERSION={case['env_pin']}\n", encoding="utf-8")
        monkeypatch.setattr(show_modes, "_APPLIED_STATE", state)
        monkeypatch.setattr(show_modes, "DEPLOYMENT_DIAL", tmp_path / "deployment.yaml")

        label, _ = show_modes._current_mode()

        assert label == case["expected"]
