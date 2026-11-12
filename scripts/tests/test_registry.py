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

The network calls are what is faked: `list_tags`, `list_releases` and `marked_latest_release` are repointed at fixed data, so what is pinned here is the selection, not the registry's or GitHub's current contents.
"""

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

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


class MarkedLatestReleaseCase(TypedDict):
    """What GitHub answers an anonymous lookup and what comes back."""

    body: bytes
    expected: str | None
    raises: str
    status: int


class OrasCommandCase(TypedDict):
    """The tools on PATH and how the oras command starts."""

    expected: list[str]
    on_path: set[str]


class _GithubAnswers(http.server.BaseHTTPRequestHandler):
    """Answer each path from the server's table, 404 for anything else."""

    def do_GET(self) -> None:  # noqa: N802
        status, body = self.server.answers.get(self.path, (404, b"{}"))
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


MARKED_LATEST_RELEASE_CASES = [
    MarkedLatestReleaseCase(
        body=b'{"tag_name": "v1.2.3"}', expected="v1.2.3", raises="", status=200
    ),
    MarkedLatestReleaseCase(body=b"{}", expected=None, raises="", status=404),
    MarkedLatestReleaseCase(
        body=b"{}", expected=None, raises="GitHub answered 403", status=403
    ),
    MarkedLatestReleaseCase(
        body=b"<html>", expected=None, raises="cannot read", status=200
    ),
]
ORAS_COMMAND_CASES = [
    OrasCommandCase(expected=["oras"], on_path={"docker", "oras"}),
    OrasCommandCase(expected=["docker", "run", "--rm", "--user"], on_path={"docker"}),
]


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[http.server.HTTPServer]:
    """A local HTTP server standing where GitHub's API is, with no `gh` on PATH."""

    def no_gh(tool: str) -> None:
        return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GithubAnswers)
    server.answers = {}
    thread = threading.Thread(daemon=True, target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(
        _registry, "_GITHUB_API", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setattr(_registry.shutil, "which", no_gh)
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


class TestMarkedLatestRelease:
    @pytest.mark.parametrize("case", MARKED_LATEST_RELEASE_CASES)
    def test_matches_expected(
        self, github: http.server.HTTPServer, case: MarkedLatestReleaseCase
    ) -> None:
        github.answers["/repos/o/r/releases/latest"] = (case["status"], case["body"])

        if case["raises"]:
            with pytest.raises(_registry.RegistryError, match=case["raises"]):
                _registry.marked_latest_release(repo="o/r")
            return

        assert _registry.marked_latest_release(repo="o/r") == case["expected"]


class TestListReleases:
    def test_anonymous_lookup_reads_every_page(
        self, github: http.server.HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_registry, "_GITHUB_PAGE_SIZE", 2)
        github.answers["/repos/o/r/releases?per_page=2&page=1"] = (
            200,
            b'[{"tag_name": "v3"}, {"tag_name": "v2"}]',
        )
        github.answers["/repos/o/r/releases?per_page=2&page=2"] = (
            200,
            b'[{"tag_name": "v1"}]',
        )

        tags = [release["tag_name"] for release in _registry.list_releases(repo="o/r")]

        assert tags == ["v3", "v2", "v1"]

    def test_a_missing_repo_raises(self, github: http.server.HTTPServer) -> None:
        with pytest.raises(_registry.RegistryError, match="no readable releases"):
            _registry.list_releases(repo="o/absent")


class TestImagesVersion:
    def test_the_digest_follows_the_pins_not_their_order(self) -> None:
        first = _registry.images_version(pins=["B=2", "A=1"], version="2.2.1")
        reordered = _registry.images_version(pins=["A=1", "B=2"], version="2.2.1")
        changed = _registry.images_version(pins=["A=1", "B=3"], version="2.2.1")

        assert first.startswith("2.2.1+images.")
        assert first == reordered
        assert first != changed

    def test_it_is_not_a_rankable_version(self) -> None:
        version = _registry.images_version(pins=["A=1"], version="2.2.1")

        with pytest.raises(_registry.RegistryError, match="not a semver tag"):
            _registry._sort_key(version=version)


def _tools_on_path(*, monkeypatch: pytest.MonkeyPatch, on_path: set[str]) -> None:
    def _which(tool: str) -> str | None:
        return f"/bin/{tool}" if tool in on_path else None

    monkeypatch.setattr(_registry.shutil, "which", _which)


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


_DIGEST = "sha256:" + "0f" * 32


@pytest.mark.parametrize(
    "stdout",
    [
        '{"digest": "sha256:short"}',
        '{"digest": "sha256:' + "0F" * 32 + '"}',
        '{"digest": "sha512:' + "0f" * 64 + '"}',
        '{"digest": ""}',
        '{"digest": 7}',
        '{"mediaType": "application/vnd.oci.image.manifest.v1+json"}',
        "not json",
    ],
    ids=[
        "short",
        "uppercase",
        "other-algorithm",
        "empty",
        "not-a-string",
        "missing",
        "unreadable",
    ],
)
def test_a_digest_not_of_the_sha256_shape_is_refused(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    _fixed_descriptor(stdout, monkeypatch)

    with pytest.raises(_registry.RegistryError, match="returned no digest"):
        _registry.resolve_digest(reference=f"{_MANIFEST}:2.2.1")


def test_a_sha256_digest_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_descriptor(f'{{"digest": "{_DIGEST}", "size": 512}}', monkeypatch)

    assert _registry.resolve_digest(reference=f"{_MANIFEST}:2.2.1") == _DIGEST


def _fixed_descriptor(stdout: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def _oras_command() -> list[str]:
        return ["oras"]

    def _run(*, cmd: list[str]) -> _registry.subprocess.CompletedProcess:
        return _registry.subprocess.CompletedProcess(
            args=cmd, returncode=0, stderr="", stdout=stdout
        )

    monkeypatch.setattr(_registry, "oras_command", _oras_command)
    monkeypatch.setattr(_registry, "_run", _run)


class TestOrasCommand:
    @pytest.mark.parametrize("case", ORAS_COMMAND_CASES)
    def test_matches_expected(
        self, monkeypatch: pytest.MonkeyPatch, case: OrasCommandCase
    ) -> None:
        _tools_on_path(monkeypatch=monkeypatch, on_path=case["on_path"])

        command = _registry.oras_command()

        assert command[: len(case["expected"])] == case["expected"]
        assert (command[-1] == _registry.ORAS_IMAGE) == (command[0] == "docker")
        assert "@sha256:" in _registry.ORAS_IMAGE

    def test_a_pull_mounts_its_output_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _tools_on_path(monkeypatch=monkeypatch, on_path={"docker"})

        command = _registry.oras_command(mount=tmp_path)

        assert f"{tmp_path}:{_registry.OUT_DIR}" in command

    def test_no_oras_and_no_docker_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tools_on_path(monkeypatch=monkeypatch, on_path=set())

        with pytest.raises(
            _registry.RegistryError, match="neither `oras` nor `docker`"
        ):
            _registry.oras_command()
