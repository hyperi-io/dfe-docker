#  Project:      dfe-docker
#  File:         tests/test_stack.py
#  Purpose:      Assert VERSION=latest repins every image we publish, and nothing else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`make stack VERSION=latest` repins the DFE images on top of a certified stack.

Which keys that covers is DERIVED from docker-compose.yml rather than listed, so the test runs against the real compose file: a service added to the stack has to turn up here without anyone editing a list, and a third-party image must never turn up at all -- floating ClickHouse or Kafka is the thing this mode is not.

The repin itself is asserted against fixed tags and digests, so what is pinned is the shape of the line written into `.env`, not GHCR's current contents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _registry
import stack

# Every image published to ghcr.io/hyperi-io that docker-compose.yml tags with a
# pin key. A new DFE service is expected to break this and be added.
_OUR_KEYS = {
    "DFE_ARCHIVER_VERSION",
    "DFE_ENGINE_VERSION",
    "DFE_FETCHER_VERSION",
    "DFE_HYPERDX_VERSION",
    "DFE_LOADER_VERSION",
    "DFE_RECEIVER_VERSION",
    "DFE_TRANSFORM_VECTOR_VERSION",
    "DFE_TRANSFORM_VRL_VERSION",
    "DFE_UI_VERSION",
}

# Third-party pins the certified stack owns. Floating any of these is what the
# scope decision ruled out.
_THIRD_PARTY_KEYS = {
    "APACHE_KAFKA_VERSION",
    "CLICKHOUSE_VERSION",
    "DFE_OTEL_COLLECTOR_VERSION",
    "DFE_PROXY_VERSION",
    "HYPERDX_FERRETDB_VERSION",
    "HYPERDX_POSTGRES_VERSION",
    "KAFBAT_VERSION",
    "REDPANDA_VERSION",
}


def test_the_repo_map_covers_every_image_we_publish() -> None:
    assert set(stack._our_image_repos()) == _OUR_KEYS


def test_the_repo_map_excludes_every_third_party_image() -> None:
    assert _THIRD_PARTY_KEYS.isdisjoint(stack._our_image_repos())


def test_a_key_maps_to_its_own_ghcr_repo() -> None:
    repos = stack._our_image_repos()

    assert repos["DFE_ENGINE_VERSION"] == "ghcr.io/hyperi-io/dfe-engine"
    assert repos["DFE_TRANSFORM_VRL_VERSION"] == "ghcr.io/hyperi-io/dfe-transform-vrl"


def test_an_image_registry_override_repoints_every_repo(dotenv: Path) -> None:
    dotenv.write_text("IMAGE_REGISTRY=registry.local/mirror\n", encoding="utf-8")

    repos = stack._our_image_repos()

    assert repos["DFE_ENGINE_VERSION"] == "registry.local/mirror/dfe-engine"
    assert repos["DFE_UI_VERSION"] == "registry.local/mirror/dfe-ui"


def test_a_repin_line_carries_the_tag_the_digest_and_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _latest_tag(*, prereleases: str = "exclude", repo: str) -> str:
        return "v1.19.36"

    def _resolve_digest(*, reference: str) -> str:
        return "sha256:" + "a" * 64

    monkeypatch.setattr(stack, "latest_tag", _latest_tag)
    monkeypatch.setattr(stack, "resolve_digest", _resolve_digest)

    pins = stack._latest_image_pins(prereleases="fallback")

    assert set(pins) == _OUR_KEYS
    assert pins["DFE_ENGINE_VERSION"] == (
        f"DFE_ENGINE_VERSION=v1.19.36@sha256:{'a' * 64}"
        "  # ghcr.io/hyperi-io/dfe-engine, newest published"
    )


def test_a_repin_never_writes_a_floating_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    def _latest_tag(*, prereleases: str = "exclude", repo: str) -> str:
        return "v1.19.36"

    def _resolve_digest(*, reference: str) -> str:
        return "sha256:" + "b" * 64

    monkeypatch.setattr(stack, "latest_tag", _latest_tag)
    monkeypatch.setattr(stack, "resolve_digest", _resolve_digest)

    for line in stack._latest_image_pins(prereleases="fallback").values():
        value = line.split("=", 1)[1].split("  #", 1)[0]
        assert "@sha256:" in value
        assert not value.startswith("latest")


def test_the_discovery_words_are_the_two_the_makefile_documents() -> None:
    assert _registry.DISCOVERY_WORDS == {"latest": "fallback", "rc": "include"}
    assert set(_registry.DISCOVERY_WORDS.values()) <= set(_registry.PRERELEASES)
