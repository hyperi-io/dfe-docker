#  Project:      dfe-docker
#  File:         tests/test_resolve_profile.py
#  Purpose:      Assert kafka-init pre-creates a transform's sink only when it has work
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which topics a profile hands kafka-init.

An idle transform is required to name a sink it never writes to, and pre-creating
that topic silences the loader: scalo drops `<base>_land` from an auto-discovered
subscription whenever a `<base>_load` sibling exists. So the property worth
pinning is one-directional -- a transform's sink is pre-created when the transform
subscribes to something, and never when it does not.

Both halves are asserted against synthetic configs and against the profiles this
repo actually ships, because the shipped ones are what a stack boots on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import resolve_profile
from _common import SERVICE_PROFILES_FILE

_IDLE = """\
source:
  brokers:
    - kafka:9092
  topics: []
sink:
  brokers:
    - kafka:9092
  topic: main_load
"""
_WORKING = """\
source:
  brokers:
    - kafka:9092
  topics:
    - main_land
sink:
  brokers:
    - kafka:9092
  topic: main_load
"""
# The dfe-transform-vector shape: one source name, from which the app derives
# `<source>_land` in and `<source>_load` out.
_SOURCE_NAMED = """\
dfe_source: filebeat
source:
  brokers: [kafka:9092]
sink:
  brokers: [kafka:9092]
"""


@pytest.fixture
def configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write the three transform configs into a throwaway config directory."""
    for name, text in (
        ("idle.yaml", _IDLE),
        ("working.yaml", _WORKING),
        ("source-named.yaml", _SOURCE_NAMED),
    ):
        (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")
    monkeypatch.setattr(resolve_profile, "CONFIG_DIR", tmp_path)
    return tmp_path


def _shipped(profile: str) -> dict:
    """Return the services block of one profile as service_profiles.yaml ships it."""
    data = resolve_profile._parse_yaml(
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
    )
    return data["profiles"][profile]["services"]


def test_an_idle_transform_pre_creates_no_sink_topic(configs: Path) -> None:
    topics = resolve_profile._init_topics(
        services={"dfe-transform-vrl": {"config_path": "idle.yaml"}}
    )

    assert topics == ["main_land"]


def test_a_working_transform_pre_creates_both_ends(configs: Path) -> None:
    topics = resolve_profile._init_topics(
        services={"dfe-transform-vrl": {"config_path": "working.yaml"}}
    )

    assert topics == ["main_land", "main_load"]


def test_a_source_named_transform_is_not_idle(configs: Path) -> None:
    topics = resolve_profile._init_topics(
        services={"dfe-transform-vector": {"config_path": "source-named.yaml"}}
    )

    assert topics == ["filebeat_land", "filebeat_load", "main_land"]


def test_only_a_transform_contributes_topics(configs: Path) -> None:
    # A loader naming a topic is a consumer, not a producer: pre-creating what it
    # subscribes to would create the very `_load` topic that suppresses `_land`.
    topics = resolve_profile._init_topics(
        services={"dfe-loader": {"config_path": "working.yaml"}}
    )

    assert topics == ["main_land"]


def test_the_shipped_single_tier_pre_creates_no_load_topic() -> None:
    topics = resolve_profile._init_topics(services=_shipped("single"))

    assert topics == ["main_land"]


def test_a_shipped_transform_profile_keeps_its_sink() -> None:
    topics = resolve_profile._init_topics(services=_shipped("kafka-filebeat"))

    assert topics == ["filebeat_land", "filebeat_load", "main_land", "main_load"]
