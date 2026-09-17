#  Project:      dfe-docker
#  File:         tests/test_app_config.py
#  Purpose:      Assert only the projected tiers read the engine-rendered app config
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which tiers hand their app config to dfe-engine, and which keep their own.

The projected tiers mirror the Kubernetes ones: their app set comes from
apps.yaml and every app in it idles until something configures it, so the engine
renders their config the way a chart renders a ConfigMap. Every other profile is
a hand-crafted data-plane shape that ships the config it exists to exercise, and
a compile from the sources would overwrite it -- which is what these assert.

Resolved by RUNNING the resolver, so what is pinned is the file a stack boots on
rather than a restatement of the rule.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import resolve_profile
from _common import PROJECTED_PROFILES


def _resolved(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """Run the resolver for one profile and read back the assignments it wrote."""
    written = tmp_path / ".profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    monkeypatch.setattr(resolve_profile, "_load_dotenv", lambda: None)
    monkeypatch.setitem(os.environ, "DFE_PROFILE", profile)
    assert resolve_profile.main() == 0
    values: dict[str, str] = {}
    for line in written.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


@pytest.mark.parametrize("profile", PROJECTED_PROFILES)
def test_a_projected_tier_has_the_engine_render_its_app_config(
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


def test_a_hand_crafted_profile_keeps_its_committed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _resolved("kafka-filebeat", tmp_path, monkeypatch)

    assert values["DFE_ENGINE_APP_CONFIG_DIR"] == ""
    assert values["DFE_RECEIVER_CONFIG_FILE"] == ""
    assert values["DFE_TRANSFORM_VRL_CONFIG"] == "transform-vrl/main.yaml"


def test_an_app_the_tier_does_not_run_is_given_no_rendered_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The slim tier runs no transform, so a path into a directory the engine
    # renders nothing into would point the container at a file that never exists.
    values = _resolved("slim", tmp_path, monkeypatch)

    assert values["DFE_TRANSFORM_VRL_CONFIG_FILE"] == ""


@pytest.mark.parametrize("profile", [*PROJECTED_PROFILES, "kafka-filebeat"])
def test_every_app_config_key_is_written_whatever_the_answer(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A line that vanished with its own value would make the included makefile
    # re-settle on every pass, which restarts `make` forever.
    values = _resolved(profile, tmp_path, monkeypatch)

    expected = {
        resolve_profile.ENGINE_APP_CONFIG_DIR_VAR,
        resolve_profile.ENGINE_APP_CONFIG_BASE_DIR_VAR,
        resolve_profile.APP_CONFIG_MOUNT_VAR,
        *resolve_profile.SERVICE_TO_RENDERED_CONFIG_VAR.values(),
    }
    assert expected <= set(values)
