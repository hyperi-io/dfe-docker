#  Project:      dfe-docker
#  File:         tests/test_helper_env.py
#  Purpose:      Assert the helper scripts read a stack's address and credentials the way make hands them over
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What the Makefile's exports look like to the helpers, and where they dial.

`export CLICKHOUSE_HTTP_PORT` of a key .env never set hands the script an EMPTY
variable, not a missing one, so a default has to survive the empty string. And a
second stack published beside another is found through DFE_POST_HOST, which every
helper has to follow or it reads the first stack's data.
"""

from __future__ import annotations

import pytest

import _pipeline
import test_source


def test_an_empty_exported_clickhouse_key_takes_its_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLICKHOUSE_HTTP_PORT", "")
    monkeypatch.setenv("CLICKHOUSE_USERNAME", "")
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "issued")

    env = test_source._suite_env(host="localhost", engine_url="http://localhost:8003")

    assert env["DFE_E2E_CH_PORT"] == "8123"
    assert env["DFE_E2E_CH_USER"] == "default"


def test_clickhouse_is_read_on_the_address_the_stack_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLICKHOUSE_URL", raising=False)
    monkeypatch.setenv("CLICKHOUSE_HTTP_PORT", "")
    monkeypatch.setenv("DFE_POST_HOST", "127.0.0.2")

    assert _pipeline.clickhouse_url() == "http://127.0.0.2:8123"


def test_one_stack_on_the_box_still_reads_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("CLICKHOUSE_URL", "CLICKHOUSE_HTTP_PORT", "DFE_POST_HOST"):
        monkeypatch.delenv(key, raising=False)

    assert _pipeline.clickhouse_url() == "http://localhost:8123"


def test_an_explicit_clickhouse_url_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLICKHOUSE_URL", "http://warehouse.example.test:8123")
    monkeypatch.setenv("DFE_POST_HOST", "127.0.0.2")

    assert _pipeline.clickhouse_url() == "http://warehouse.example.test:8123"
