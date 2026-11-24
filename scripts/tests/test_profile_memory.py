#  Project:      dfe-docker
#  File:         tests/test_profile_memory.py
#  Purpose:      Assert the README and `make help` state the same memory for every profile
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Each rendered profile has a row in the README memory table and the same two figures in the Makefile help."""

import re

import pytest
from _common import REPO_ROOT, RENDERED_PROFILES

_ROW = re.compile(
    r"^\| `(?P<name>[a-z]+)` \| \d+ \| (?P<working>[\d.]+) GiB \| (?P<limits>[\d.]+) GiB \|$",
    re.M,
)


def _readme_rows() -> dict[str, tuple[str, str]]:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    return {m["name"]: (m["working"], m["limits"]) for m in _ROW.finditer(text)}


def test_the_readme_has_a_row_for_every_profile_and_no_other() -> None:
    assert sorted(_readme_rows()) == sorted(RENDERED_PROFILES)


@pytest.mark.parametrize("profile", RENDERED_PROFILES)
def test_make_help_states_the_figures_the_readme_does(profile: str) -> None:
    working, limits = _readme_rows()[profile]
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    line = next(text for text in makefile.splitlines() if f"'{profile}' '" in text)

    assert f"{working} GiB working set" in line
    assert f"{limits} GiB of limits" in line
