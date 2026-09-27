#  Project:      dfe-docker
#  File:         tests/test_e2e_expected_http.py
#  Purpose:      Assert the e2e HTTP checks reach the stack under test
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The e2e HTTP checks follow the ports compose publishes the stack on.

A literal port in a check URL probes whatever holds that port, which beside
another stack on the same daemon is the other stack.
"""

import re

import pytest

import _pipeline
from _common import REPO_ROOT

_CHECKS = REPO_ROOT / "tests" / "e2e" / "e2e-tests.yaml"


def test_a_moved_port_reaches_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFE_UI_PORT", "47300")

    got = _pipeline.expand_env("http://localhost:${DFE_UI_PORT:-3000}/livez")

    assert got == "http://localhost:47300/livez"


@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_or_empty_port_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("DFE_UI_PORT", raising=False)
    else:
        monkeypatch.setenv("DFE_UI_PORT", value)

    got = _pipeline.expand_env("http://localhost:${DFE_UI_PORT:-3000}/")

    assert got == "http://localhost:3000/"


def test_text_with_no_reference_is_unchanged() -> None:
    assert _pipeline.expand_env("http://localhost:3000/") == "http://localhost:3000/"


def test_no_check_url_names_a_literal_local_port() -> None:
    urls = re.findall(r"url:\s*(\S+)", _CHECKS.read_text(encoding="utf-8"))

    literal = [url for url in urls if re.match(r"https?://[^/]+:\d", url)]

    assert urls
    assert literal == []
