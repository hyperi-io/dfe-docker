#  Project:      dfe-docker
#  File:         scripts/_registry.py
#  Purpose:      Discover the newest published tag on an OCI repo and resolve its digest
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Registry tag discovery for the pin tooling.

Internal support module - imported by the runnable scripts, not executed directly. Two callers ask a registry "what is the newest thing published here": `make stack VERSION=latest` asks it of each DFE image repo, and the track-latest daemon asks it of the signed stack-manifest repo. Both answers come from `oras`, which the stack already requires.

Only semver tags are ranked, so the `sha-<commit>` build tags and the floating `latest` are ignored rather than compared. An optional leading `v` is carried through verbatim, because the tag itself is what goes into the pin. A release ranks above its own pre-releases; what happens when a repo has published only pre-releases is the caller's `prereleases` choice.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

# The signed stack-manifest artifact repo -- the certified pin set's home.
DEFAULT_MANIFEST_REPO = "ghcr.io/hyperi-io/dfe-stack-manifest"

# The VERSION= words meaning "discover the newest", mapped to how each treats a
# pre-release. Anything else is an explicit version.
#
# `latest` FALLS BACK rather than excluding, because a repo can be entirely
# pre-release: dfe-stack-manifest has published nothing but -rc.N, so a
# stable-only `latest` resolves to nothing there.
DISCOVERY_WORDS = {"latest": "fallback", "rc": "include"}

# What `latest_tag` does with a pre-release tag.
PRERELEASES = ("exclude", "fallback", "include")

# X.Y.Z with an optional `v` prefix and an optional -rc.N / -beta.N suffix.
_SEMVER = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)


class RegistryError(Exception):
    """Raised when a registry cannot be listed or a reference cannot be resolved."""


def _is_stable(*, version: str) -> bool:
    """Whether a tag is a release rather than a pre-release."""
    match = _SEMVER.match(version)
    return match is not None and match["pre"] is None


def _pre_key(*, pre: str) -> tuple:
    """Pre-release precedence: all-digit identifiers compare numerically and rank below alphanumeric ones."""
    return tuple(
        (0, int(ident), "") if ident.isdigit() else (1, 0, ident)
        for ident in pre.split(".")
    )


def _require_oras() -> None:
    """Fail loudly when `oras` is absent rather than degrading to a floating tag."""
    if shutil.which("oras") is None:
        raise RegistryError(
            "`oras` is not installed -- cannot read tags or digests from the "
            "registry (see the README prerequisites)"
        )


def _run(*, cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a tool capturing text output as explicit UTF-8.

    ``text=True`` alone uses the locale encoding, so one non-ASCII byte under
    ``LC_ALL=C`` would raise ``UnicodeDecodeError`` rather than degrade.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )


def _sort_key(*, version: str) -> tuple:
    """Semver sort key. A release ranks ABOVE its own pre-releases."""
    match = _SEMVER.match(version)
    if match is None:
        raise RegistryError(f"not a semver tag: {version!r}")
    core = (int(match["major"]), int(match["minor"]), int(match["patch"]))
    return (
        (*core, 1) if match["pre"] is None else (*core, 0, _pre_key(pre=match["pre"]))
    )


def _tag_order(tag: str) -> tuple:
    """Key function for ``max`` over a tag list."""
    return _sort_key(version=tag)


def latest_tag(*, prereleases: str = "exclude", repo: str) -> str:
    """Return the highest semver tag published on ``repo``, verbatim.

    ``prereleases`` is one of ``PRERELEASES``: ``exclude`` ranks releases only,
    ``include`` ranks everything, ``fallback`` prefers a release and takes the
    newest pre-release when the repo has published no release at all.
    """
    if prereleases not in PRERELEASES:
        raise RegistryError(f"prereleases={prereleases!r} is not one of {PRERELEASES}")
    tags = [tag for tag in list_tags(repo=repo) if _SEMVER.match(tag)]
    if not (tags):
        raise RegistryError(f"no semver tags found on {repo}")
    releases = [tag for tag in tags if _is_stable(version=tag)]
    if prereleases == "exclude":
        if not (releases):
            raise RegistryError(f"no stable semver tags found on {repo}")
        return max(releases, key=_tag_order)
    if prereleases == "fallback" and releases:
        return max(releases, key=_tag_order)
    return max(tags, key=_tag_order)


def list_tags(*, repo: str) -> list[str]:
    """Return every tag on an OCI repo via `oras repo tags`."""
    _require_oras()
    out = _run(cmd=["oras", "repo", "tags", repo])
    if out.returncode != 0:
        detail = out.stderr.strip() or out.stdout.strip()
        raise RegistryError(f"`oras repo tags {repo}` failed: {detail}")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def manifest_repo() -> str:
    """Return the stack-manifest repo, honouring the DFE_STACK_MANIFEST_REPO override."""
    return (
        os.environ.get("DFE_STACK_MANIFEST_REPO", "").strip() or DEFAULT_MANIFEST_REPO
    )


def resolve_digest(*, reference: str) -> str:
    """Return the ``sha256:...`` digest a tagged reference currently points at.

    A tag is a moving pointer, so the digest is what makes the pin reproducible --
    every pin this repo writes carries one.
    """
    _require_oras()
    out = _run(cmd=["oras", "manifest", "fetch", "--descriptor", reference])
    if out.returncode != 0:
        detail = out.stderr.strip() or out.stdout.strip()
        raise RegistryError(f"cannot resolve {reference}: {detail}")
    try:
        return json.loads(out.stdout)["digest"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RegistryError(
            f"`oras manifest fetch --descriptor {reference}` returned no digest: "
            f"{out.stdout.strip()!r}"
        ) from error
