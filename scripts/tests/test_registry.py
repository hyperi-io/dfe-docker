#  Project:      dfe-docker
#  File:         tests/test_registry.py
#  Purpose:      Assert tag and release discovery pick what a pin needs from real shapes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`make stack VERSION=latest` picks one version out of whatever a repo happens to publish.

The stack manifest is picked by TAG. An OCI repo carries `sha-<commit>` build tags and a floating `latest` beside the semver ones, so the ranking has to ignore what it cannot compare rather than choke on it or rank it. The `v` prefix has to survive, because the tag string is what goes into the pin and `dfe-engine:1.19.36` does not exist.

A DFE image is picked by RELEASE, and version order is not release order there: a release tagged above the line it sits in must not win.

The network calls are the seam: `list_tags`, `list_releases` and `marked_latest_release` are repointed at fixed data, so what is pinned here is the selection, not the registry's or GitHub's current contents.
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


# Four of dfe-hyperdx's real releases, newest first as `gh api` returns them: its
# v1.0.0 was created before every other release and outranks them by version.
_HYPERDX = "hyperi-io/dfe-hyperdx"
_HYPERDX_RELEASES = [
    {"tag_name": "v0.2.7", "draft": False, "created_at": "2026-09-26T15:42:24Z"},
    {"tag_name": "v0.2.6", "draft": False, "created_at": "2026-09-26T13:57:37Z"},
    {"tag_name": "v0.0.1", "draft": False, "created_at": "2026-07-07T14:12:25Z"},
    {"tag_name": "v1.0.0", "draft": False, "created_at": "2026-02-20T11:22:18Z"},
]


def _fixed_releases(
    releases: list[dict], marked: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _list_releases(*, repo: str) -> list[dict]:
        return releases

    def _marked_latest_release(*, repo: str) -> str | None:
        return marked

    monkeypatch.setattr(_registry, "list_releases", _list_releases)
    monkeypatch.setattr(_registry, "marked_latest_release", _marked_latest_release)


def test_the_release_github_marks_latest_wins_over_a_higher_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_releases(_HYPERDX_RELEASES, "v0.2.7", monkeypatch)

    for mode in ("exclude", "fallback"):
        assert _registry.latest_release(prereleases=mode, repo=_HYPERDX) == "v0.2.7"


def test_including_prereleases_ranks_by_creation_not_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_releases(_HYPERDX_RELEASES, "v0.2.7", monkeypatch)

    assert _registry.latest_release(prereleases="include", repo=_HYPERDX) == "v0.2.7"


def test_a_prerelease_newer_than_latest_is_taken_only_when_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rc = {
        "tag_name": "v0.3.0-rc.1",
        "draft": False,
        "created_at": "2026-09-27T01:00:00Z",
    }
    _fixed_releases([rc, *_HYPERDX_RELEASES], "v0.2.7", monkeypatch)

    assert _registry.latest_release(prereleases="fallback", repo=_HYPERDX) == "v0.2.7"
    assert (
        _registry.latest_release(prereleases="include", repo=_HYPERDX) == "v0.3.0-rc.1"
    )


def test_fallback_takes_the_newest_prerelease_when_nothing_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_releases(
        [
            {"tag_name": "v2.0.0-rc.2", "draft": False, "created_at": "2026-09-02"},
            {"tag_name": "v2.0.0-rc.10", "draft": False, "created_at": "2026-09-20"},
        ],
        None,
        monkeypatch,
    )

    assert (
        _registry.latest_release(prereleases="fallback", repo=_HYPERDX)
        == "v2.0.0-rc.10"
    )
    with pytest.raises(_registry.RegistryError, match="no release marked Latest"):
        _registry.latest_release(repo=_HYPERDX)


def test_a_draft_never_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = {"tag_name": "v0.2.8", "draft": True, "created_at": "2026-09-27T02:00:00Z"}
    _fixed_releases([draft, *_HYPERDX_RELEASES], "v0.2.7", monkeypatch)

    assert _registry.latest_release(prereleases="include", repo=_HYPERDX) == "v0.2.7"


def test_a_repo_with_no_published_release_fails_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = {"tag_name": "v0.1.0", "draft": True, "created_at": "2026-09-27T02:00:00Z"}
    _fixed_releases([draft], None, monkeypatch)

    for mode in ("fallback", "include"):
        with pytest.raises(_registry.RegistryError, match="has published no release"):
            _registry.latest_release(prereleases=mode, repo=_HYPERDX)


def test_an_unknown_prerelease_mode_is_refused_for_releases_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_releases(_HYPERDX_RELEASES, "v0.2.7", monkeypatch)

    with pytest.raises(_registry.RegistryError, match="is not one of"):
        _registry.latest_release(prereleases="maybe", repo=_HYPERDX)


def test_the_manifest_repo_honours_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _registry.manifest_repo() == _registry.DEFAULT_MANIFEST_REPO

    monkeypatch.setenv("DFE_STACK_MANIFEST_REPO", "registry.local/mirror/dfe-stack")
    assert _registry.manifest_repo() == "registry.local/mirror/dfe-stack"
