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

A key a helper reads from env/engine.env comes from that file or the environment, never from the stale copy an older .env may still hold.
"""

import os
from pathlib import Path

import pytest

import _common
import _pipeline
import test_source

# A key the helpers read from env/engine.env, never from .env
_ENGINE_KEY = "DFE_HELPER_ENV_TEST_PASSWORD"


@pytest.fixture
def unset_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Return `_ENGINE_KEY`, absent from the environment and absent again once the test ends."""
    monkeypatch.setenv(_ENGINE_KEY, "")
    monkeypatch.delenv(_ENGINE_KEY)
    return _ENGINE_KEY


def test_an_empty_exported_clickhouse_key_takes_its_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLICKHOUSE_HTTP_PORT", "")
    monkeypatch.setenv("CLICKHOUSE_USERNAME", "")
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "issued")

    env = test_source._suite_env(
        engine_url="http://localhost:8003", found={}, host="localhost"
    )

    assert env["DFE_E2E_CH_PORT"] == "8123"
    assert env["DFE_E2E_CH_USER"] == "default"


def test_the_runner_starts_make_without_the_keys_dotenv_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # make re-exports an inherited .env key with the spaces before its comment
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "issued")
    monkeypatch.setenv("DFE_RECEIVER_VERSION", "v1.15.57@sha256:abc")

    env = test_source._suite_env(
        engine_url="http://localhost:8003",
        found={"PATH": "/usr/bin"},
        host="localhost",
    )

    assert "DFE_RECEIVER_VERSION" not in env
    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["DFE_E2E_ENGINE_PASSWORD"] == "issued"


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


def test_a_key_in_both_files_is_read_from_the_engine_env(
    dotenv: Path, engine_env: Path, unset_key: str
) -> None:
    dotenv.write_text(f"{unset_key}=older\n", encoding="utf-8")
    engine_env.write_text(f"{unset_key}=current\n", encoding="utf-8")

    _common._load_env_file(component=_common.ENGINE_COMPONENT, keys=(unset_key,))
    _common._load_dotenv()

    assert os.environ[unset_key] == "current"


def test_a_key_only_an_older_dotenv_holds_is_not_read(
    dotenv: Path, engine_env: Path, unset_key: str
) -> None:
    dotenv.write_text(f"{unset_key}=older\n", encoding="utf-8")
    engine_env.write_text("OTHER=value\n", encoding="utf-8")

    _common._load_env_file(component=_common.ENGINE_COMPONENT, keys=(unset_key,))
    _common._load_dotenv()

    assert os.environ[unset_key] == ""


def test_a_key_neither_file_holds_reads_empty(
    dotenv: Path, engine_env: Path, unset_key: str
) -> None:
    dotenv.write_text(f"# {unset_key}=example\n", encoding="utf-8")
    engine_env.write_text(f"# {unset_key}=example\n", encoding="utf-8")

    _common._load_env_file(component=_common.ENGINE_COMPONENT, keys=(unset_key,))
    _common._load_dotenv()

    assert os.environ[unset_key] == ""


def test_the_environment_beats_the_engine_env(
    engine_env: Path, monkeypatch: pytest.MonkeyPatch, unset_key: str
) -> None:
    monkeypatch.setenv(unset_key, "from-the-shell")
    engine_env.write_text(f"{unset_key}=current\n", encoding="utf-8")

    _common._load_env_file(component=_common.ENGINE_COMPONENT, keys=(unset_key,))

    assert os.environ[unset_key] == "from-the-shell"
