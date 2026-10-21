#  Project:      dfe-docker
#  File:         tests/test_app_config.py
#  Purpose:      Assert every profile reads the engine-rendered app config while the core footprint runs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""When a stack hands its app config to dfe-engine and when each app keeps its committed one.

On every profile the engine renders the app config the way a chart renders a ConfigMap. With the core footprint off there is no engine to render it, so each app reads its committed file.

Resolved by RUNNING the resolver, so what is pinned is the file a stack boots on
rather than a restatement of the rule.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import resolve_profile
from _common import COMPOSE_FILE, RENDERED_PROFILES


def _compose_service_block(service: str) -> str:
    """Return the lines of one compose service block, its own header excluded.

    A text read rather than a YAML parse: these tests run with no PyYAML, the
    same as the scripts they cover. Services are indented two spaces and their
    keys four, so the block ends at the next two-space key.
    """
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(
        rf"^  {re.escape(service)}:$\n(.*?)(?=^  \S)", text, re.MULTILINE | re.DOTALL
    )
    assert match, f"{service} is not a service in {COMPOSE_FILE.name}"
    return match.group(1)


@pytest.mark.parametrize(
    "service,var", sorted(resolve_profile.SERVICE_TO_RENDERED_CONFIG_VAR.items())
)
def test_a_service_given_a_rendered_path_can_reach_and_read_it(
    service: str, var: str
) -> None:
    """The resolver naming a path is half the wiring; the container needs both halves.

    A service missing either one reads its committed config and reports healthy
    doing it, so the engine writes a config nothing acts on and no check fires.
    """
    block = _compose_service_block(service)

    volume = resolve_profile.APP_CONFIG_VOLUME
    assert f"${{{var}:-" in block, (
        f"{service} never reads {var}, so the engine-rendered config cannot reach it"
    )
    assert f"- {volume}:{resolve_profile.APP_CONFIG_MOUNT}:ro" in block, (
        f"{service} does not mount {volume}, so the path {var} names does not "
        "exist inside the container"
    )


def _resolved(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """Run the resolver for one profile and read back the assignments it wrote."""
    written = tmp_path / ".tmp" / "profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    monkeypatch.setattr(resolve_profile, "_load_dotenv", lambda: None)
    monkeypatch.setitem(os.environ, "DFE_PROFILE", profile)
    assert resolve_profile.main() == 0
    values: dict[str, str] = {}
    for line in written.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


@pytest.mark.parametrize("profile", RENDERED_PROFILES)
def test_every_profile_has_the_engine_render_its_app_config(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _resolved(profile, tmp_path, monkeypatch)

    assert values["DFE_ENGINE_APP_CONFIG_DIR"] == resolve_profile.ENGINE_APP_CONFIG_DIR
    assert (
        values["DFE_ENGINE_APP_CONFIG_BASE_DIR"]
        == resolve_profile.ENGINE_APP_CONFIG_BASE_DIR
    )
    assert (
        values["DFE_RECEIVER_CONFIG_FILE"]
        == f"{resolve_profile.APP_CONFIG_MOUNT}/dfe-receiver/config.yaml"
    )


def test_a_profile_without_core_keeps_its_committed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(os.environ, "DFE_CORE_ENABLED", "false")
    values = _resolved("slim-kafka", tmp_path, monkeypatch)

    assert values["DFE_ENGINE_APP_CONFIG_DIR"] == ""
    assert values["DFE_RECEIVER_CONFIG_FILE"] == ""
    assert values["DFE_RECEIVER_CONFIG"] == "receiver/kafka.yaml"


def test_an_app_the_tier_does_not_run_is_given_no_rendered_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The slim profiles run no archiver, so a path into a directory the engine
    # renders nothing into would point the container at a file that never exists.
    values = _resolved("slim-grpc", tmp_path, monkeypatch)

    assert values["DFE_ARCHIVER_CONFIG_FILE"] == ""


@pytest.mark.parametrize("core", ["true", "false"])
@pytest.mark.parametrize("profile", RENDERED_PROFILES)
def test_every_app_config_key_is_written_whatever_the_answer(
    core: str, profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A line that vanished with its own value would make the included makefile
    # re-settle on every pass, which restarts `make` forever.
    monkeypatch.setitem(os.environ, "DFE_CORE_ENABLED", core)
    values = _resolved(profile, tmp_path, monkeypatch)

    expected = {
        resolve_profile.ENGINE_APP_CONFIG_DIR_VAR,
        resolve_profile.ENGINE_APP_CONFIG_BASE_DIR_VAR,
        resolve_profile.APP_CONFIG_MOUNT_VAR,
        *resolve_profile.SERVICE_TO_RENDERED_CONFIG_VAR.values(),
    }
    assert expected <= set(values)
