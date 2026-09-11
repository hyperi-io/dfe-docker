#  Project:      dfe-docker
#  File:         tests/test_registry.py
#  Purpose:      Assert tag discovery ranks real GHCR tag sets the way a pin needs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`make stack VERSION=latest` picks one tag out of whatever a repo happens to publish.

A DFE image repo carries `sha-<commit>` build tags and a floating `latest` beside the semver ones, so the ranking has to ignore what it cannot compare rather than choke on it or rank it. The `v` prefix has to survive, because the tag string is what goes into the pin and `dfe-engine:1.19.36` does not exist.

The network calls are the seam: `list_tags` is repointed at a fixed tag list, so what is pinned here is the selection, not GHCR's current contents.
"""

from __future__ import annotations

import pytest

import _registry

_MANIFEST = "ghcr.io/hyperi-io/dfe-stack-manifest"

# What `oras repo tags ghcr.io/hyperi-io/dfe-engine` actually returns: v-prefixed
# semver interleaved with per-commit build tags.
_DFE_TAGS = [
    "v1.19.9",
    "sha-9352c4a6",
    "v1.19.35",
    "latest",
    "v1.19.36",
    "sha-ca635f78",
]


def _fixed_tags(tags: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def _list_tags(*, repo: str) -> list[str]:
        return tags

    monkeypatch.setattr(_registry, "list_tags", _list_tags)


def test_the_newest_semver_wins_over_the_build_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_tags(_DFE_TAGS, monkeypatch)

    assert _registry.latest_tag(repo="ghcr.io/hyperi-io/dfe-engine") == "v1.19.36"


def test_patch_numbers_compare_numerically_not_lexically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_tags(["v1.19.9", "v1.19.36", "v1.19.10"], monkeypatch)

    assert _registry.latest_tag(repo="ghcr.io/hyperi-io/dfe-engine") == "v1.19.36"


def test_a_prerelease_is_skipped_unless_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_tags(["2.2.0", "2.3.0-rc.2"], monkeypatch)

    assert _registry.latest_tag(repo=_MANIFEST) == "2.2.0"
    assert _registry.latest_tag(prereleases="include", repo=_MANIFEST) == "2.3.0-rc.2"


def test_a_release_outranks_its_own_prereleases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_tags(["2.2.0-rc.12", "2.2.0", "2.2.0-rc.2"], monkeypatch)

    assert _registry.latest_tag(prereleases="include", repo=_MANIFEST) == "2.2.0"


def test_rc_numbers_compare_numerically(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_tags(["2.2.0-rc.2", "2.2.0-rc.12"], monkeypatch)

    assert _registry.latest_tag(prereleases="include", repo=_MANIFEST) == "2.2.0-rc.12"


def test_fallback_takes_a_prerelease_only_when_no_release_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # dfe-stack-manifest's real shape: every published tag is a pre-release, so
    # `latest` resolves to nothing without the fallback.
    _fixed_tags(["2.2.0-rc.11", "2.2.0-rc.12"], monkeypatch)
    assert _registry.latest_tag(prereleases="fallback", repo=_MANIFEST) == "2.2.0-rc.12"

    _fixed_tags(["2.1.0", "2.2.0-rc.12"], monkeypatch)
    assert _registry.latest_tag(prereleases="fallback", repo=_MANIFEST) == "2.1.0"


def test_exclude_fails_on_a_prerelease_only_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_tags(["2.2.0-rc.12"], monkeypatch)

    with pytest.raises(_registry.RegistryError, match="no stable semver tags"):
        _registry.latest_tag(repo=_MANIFEST)


def test_a_repo_with_no_semver_tags_fails_rather_than_picking_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_tags(["latest", "sha-ca635f78"], monkeypatch)

    for mode in _registry.PRERELEASES:
        with pytest.raises(_registry.RegistryError, match="no semver tags"):
            _registry.latest_tag(prereleases=mode, repo="ghcr.io/hyperi-io/dfe-engine")


def test_an_unknown_prerelease_mode_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_tags(_DFE_TAGS, monkeypatch)

    with pytest.raises(_registry.RegistryError, match="is not one of"):
        _registry.latest_tag(prereleases="maybe", repo=_MANIFEST)


def test_the_manifest_repo_honours_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _registry.manifest_repo() == _registry.DEFAULT_MANIFEST_REPO

    monkeypatch.setenv("DFE_STACK_MANIFEST_REPO", "registry.local/mirror/dfe-stack")
    assert _registry.manifest_repo() == "registry.local/mirror/dfe-stack"
