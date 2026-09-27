#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/stack.py
#  Purpose:      Pin the DFE stack image versions into .env from the stack SSoT
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pin the stack image versions into .env from the DFE stack SSoT.

`make stack VERSION=X.Y.Z[-rc.N]` resolves one certified pin set for the named
stack and merges its ``*_VERSION=tag@digest`` lines into ``.env``. Only those
keys are overwritten; every other key (ports, hosts, creds, profile) is left
exactly as it was, so the merge is idempotent and safe to re-run.

``VERSION=latest`` pins the certified set of the newest stack the stack-manifest
repo publishes: its newest release, or its newest pre-release while it has
published no release. ``VERSION=rc`` takes its newest tag of either kind. Both
read nothing but the public OCI registry, so they need no GitHub access and no
credential. ``.env`` records the WORD, so a re-run moves forward; that makes them
development and integration modes, never a deployment -- `make modes` reports
them as unpinned.

``DFE_STACK_REPIN_IMAGES=1`` adds the DEV CURRENCY step on top: every image we
publish to ``ghcr.io/hyperi-io`` is repinned at its component's newest GitHub
release, read through a logged-in ``gh`` that can see the component repos. That
combination is newer than any stack anyone certified. ``latest`` takes the
release GitHub marks Latest and falls back to the newest pre-release on a repo
that has published none; ``rc`` takes the newest release of either kind. The
GitHub release is the authority rather than the highest registry tag, because
version order is not release order: a fork can carry a release tagged above its
own line. The pins it writes still carry digests; nothing here ever emits a
floating tag.

Transport is EXPLICIT-LOCAL-FIRST, OCI-DEFAULT:

1. A local dfe-infra checkout, ONLY when ``DFE_INFRA_DIR`` names one (no
   implicit sibling probe): run ``scripts/dfe-stack render --target docker
   --stack VERSION`` and read its stdout fragment.
2. Else pull the signed OCI stack-manifest artifact
   (``ghcr.io/hyperi-io/dfe-stack-manifest:VERSION`` via ``oras pull``) and read
   its ``dfe-env-VERSION.txt`` member. This is the air-gap / CI path.
3. Neither available -> fail loudly. We never silently fall back to ``latest``.

The rendered fragment is the source of truth for the pins. This tool only
routes it and merges it; it does not compose version strings itself.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import (
    COMPOSE_FILE,
    DOTENV_FILE,
    DOTENV_TEMPLATE,
    _dotenv_values,
    _print,
    _rel_path,
)
from _registry import (
    DISCOVERY_WORDS,
    RegistryError,
    latest_release,
    latest_tag,
    manifest_repo,
    resolve_digest,
)

# `make stack` depends on `.env` existing, and creating one is init's job -- it is
# the only place that mints the generated secrets. A plain copy here would produce
# a .env whose secrets are still the sentinel placeholders, which starts but fails
# `make post`.
from init import _create_dotenv

# A rendered pin line: KEY=value[  # annotation]. Keys are UPPER_SNAKE ending in
# _VERSION; only such active (non-comment) lines from the fragment are merged.
_PIN_LINE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<rest>.+)$")

# Which stack the pins came from. `make modes` reports it and `VERSION ?=
# $(DFE_STACK_VERSION)` defaults from it, so it rides in `.env` beside the pins
# while naming a stack rather than an image.
STACK_VERSION_KEY = "DFE_STACK_VERSION"

# Opt-in for the GitHub-release repin, which needs `gh` read access to every
# component repo and so is never part of the default `latest` / `rc` path.
REPIN_ENV = "DFE_STACK_REPIN_IMAGES"
_TRUTHY = {"1", "true", "yes", "on"}

# A compose image line for an image WE publish: the registry default is captured
# so `.env` need not set IMAGE_REGISTRY for the repo to be known.
_OUR_IMAGE_LINE = re.compile(
    r"^\s*image:\s*\$\{IMAGE_REGISTRY:-(?P<registry>[^}]+)\}/(?P<name>[\w.-]+)"
    r":\$\{(?P<key>[A-Z][A-Z0-9_]*)[:}]"
)


class StackError(Exception):
    """Raised when a pin set cannot be resolved, rendered, or merged."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a tool capturing text output as explicit UTF-8.

    ``text=True`` alone uses the locale encoding, so one non-ASCII byte under
    ``LC_ALL=C`` would raise ``UnicodeDecodeError`` rather than degrade.
    """
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def _infra_dir() -> Path | None:
    """Resolve a local dfe-infra checkout holding the dfe-stack renderer.

    ``DFE_INFRA_DIR`` is the only local path considered, and it must be set
    explicitly -- there is no implicit sibling probe, so the default on any
    machine is the OCI manifest path. An explicitly-set but rendererless
    directory is an error (loud, not silent).
    """
    raw = os.environ.get("DFE_INFRA_DIR", "").strip()
    if not (raw):
        return None
    candidate = Path(raw).expanduser()
    if (candidate / "scripts" / "dfe-stack").is_file():
        return candidate
    raise StackError(
        f"DFE_INFRA_DIR={raw!r} has no scripts/dfe-stack renderer -- "
        "point it at a dfe-infra checkout or unset it to use the OCI path"
    )


def _render_local(infra_dir: Path, version: str) -> str:
    """Render the docker .env fragment via a local dfe-infra dfe-stack."""
    dfe_stack = infra_dir / "scripts" / "dfe-stack"
    out = _run(
        [
            sys.executable,
            str(dfe_stack),
            "render",
            "--target",
            "docker",
            "--stack",
            version,
        ]
    )
    if out.returncode != 0:
        detail = out.stderr.strip() or out.stdout.strip()
        raise StackError(f"local render failed ({dfe_stack}): {detail}")
    return out.stdout


def _render_oci(version: str) -> str:
    """Pull the signed OCI stack manifest and read its .env member (air-gap path)."""
    if shutil.which("oras") is None:
        raise StackError(
            "no local dfe-infra checkout found and `oras` is not installed -- "
            "cannot pull the OCI stack manifest (set DFE_INFRA_DIR to a "
            "dfe-infra checkout, or install oras)"
        )
    repo = manifest_repo()
    ref = f"{repo}:{version}"
    member = (
        os.environ.get("DFE_STACK_ENV_MEMBER", "").strip() or f"dfe-env-{version}.txt"
    )
    with tempfile.TemporaryDirectory(prefix="dfe-stack-") as tmp:
        out = _run(["oras", "pull", "--output", tmp, ref])
        if out.returncode != 0:
            detail = out.stderr.strip() or out.stdout.strip()
            raise StackError(f"oras pull {ref} failed: {detail}")
        member_path = Path(tmp) / member
        if not member_path.is_file():
            raise StackError(f"OCI manifest {ref} has no member {member!r}")
        return member_path.read_text(encoding="utf-8", errors="replace")


def _resolve_fragment(version: str) -> tuple[str, str]:
    """Return (rendered fragment, human-readable source description)."""
    infra = _infra_dir()
    if infra is not None:
        return _render_local(infra, version), f"local dfe-infra checkout ({infra})"
    return _render_oci(version), f"OCI stack manifest ({manifest_repo()}:{version})"


def _parse_pins(fragment: str) -> dict[str, str]:
    """Extract KEY -> full rendered line for every active ``*_VERSION`` pin line."""
    pins: dict[str, str] = {}
    for raw in fragment.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN_LINE.match(stripped)
        if not match:
            continue
        key = match.group("key")
        if not key.endswith("_VERSION"):
            continue
        pins[key] = f"{key}={match.group('rest').strip()}"
    return pins


def _refuse_undigested(pins: dict[str, str]) -> None:
    """Raise unless every pin carries a digest.

    A tag is a moving pointer, so a pin without one names whatever the registry
    serves at pull time rather than the image the stack was certified on. The
    render emits a bare tag wherever versions.yaml has no `digests` entry for the
    image, and nothing downstream can tell the difference.
    """
    bare = sorted(
        key
        for key, line in pins.items()
        # The stack marker is a version string, not an image reference.
        if key != STACK_VERSION_KEY
        and "@sha256:" not in line.split("=", 1)[1].split("  #", 1)[0]
    )
    if bare:
        raise StackError(
            f"the render produced {len(bare)} pin(s) with no digest: "
            f"{', '.join(bare)} -- add a `digests` entry for each in the stack SSoT"
        )


def _our_image_lines() -> list[re.Match[str]]:
    """Every compose image line for an image we publish, in file order."""
    lines = COMPOSE_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return [match for line in lines if (match := _OUR_IMAGE_LINE.match(line))]


def _our_image_repos() -> dict[str, str]:
    """Map each pin key to the ghcr.io/hyperi-io image repo that compose tags with it.

    Read out of docker-compose.yml rather than listed here, so a service added to
    the stack cannot silently escape the repin. ``IMAGE_REGISTRY`` in ``.env``
    wins over the compose default, which is what a registry mirror sets.
    """
    override = _dotenv_values().get("IMAGE_REGISTRY", "").strip()
    return {
        match["key"]: f"{override or match['registry']}/{match['name']}"
        for match in _our_image_lines()
    }


def _our_release_repos() -> dict[str, str]:
    """Map each pin key to the ``owner/name`` GitHub repo that releases its image.

    A GHCR namespace is its GitHub owner, and each image shares its repo's name.
    Taken from the compose default rather than ``IMAGE_REGISTRY``: a mirror moves
    where the image is pulled from, not where it is released.
    """
    return {
        match["key"]: f"{match['registry'].rsplit('/', 1)[-1]}/{match['name']}"
        for match in _our_image_lines()
    }


def _repin_requested() -> bool:
    """Whether the caller opted into repinning each DFE image at its newest GitHub release."""
    return os.environ.get(REPIN_ENV, "").strip().lower() in _TRUTHY


def _latest_image_pins(*, prereleases: str) -> dict[str, str]:
    """Return a pin line per DFE image, at its component's newest release plus digest."""
    sources = _our_release_repos()
    pins: dict[str, str] = {}
    for key, repo in sorted(_our_image_repos().items()):
        tag = latest_release(prereleases=prereleases, repo=sources[key])
        digest = resolve_digest(reference=f"{repo}:{tag}")
        pins[key] = f"{key}={tag}@{digest}  # {repo}, newest release of {sources[key]}"
    return pins


def _merge_into_env(env_text: str, pins: dict[str, str]) -> str:
    """Overwrite the pinned keys in ``env_text``; preserve every other line.

    Replaces the first matching line for each pin -- an active ``KEY=...`` line,
    else a commented ``# KEY=...`` placeholder -- so the DFE stack is the single
    authority for the version pins while ports, hosts, creds and profile stay
    untouched. Keys with no line at all are appended in a managed block.
    """
    pending = dict(pins)
    out: list[str] = []
    for line in env_text.splitlines():
        stripped = line.strip()
        matched: str | None = None
        for key in pending:
            if stripped.startswith(f"{key}="):
                matched = key
                break
        if matched is None and stripped.startswith("#"):
            body = stripped.lstrip("#").strip()
            for key in pending:
                if body.startswith(f"{key}="):
                    matched = key
                    break
        out.append(pending.pop(matched) if matched is not None else line)
    if pending:
        out.append("")
        out.append("# --- DFE stack pins (added by 'make stack') ---")
        out.extend(pins[key] for key in pins if key in pending)
    return "\n".join(out) + "\n"


def _ensure_env() -> None:
    """Make sure ``.env`` exists so there is something to merge into."""
    if DOTENV_FILE.is_file():
        return
    if not DOTENV_TEMPLATE.is_file():
        raise StackError(
            f"neither {_rel_path(path=DOTENV_FILE)} nor "
            f"{_rel_path(path=DOTENV_TEMPLATE)} exists -- run `make init` first"
        )
    _create_dotenv(dst_path=DOTENV_FILE, src_path=DOTENV_TEMPLATE)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="stack.py", description=__doc__.split("\n")[0].strip()
    )
    parser.add_argument(
        "version",
        nargs="?",
        help="stack version to pin (2.2.0, 2.2.0-rc.1), or `latest` / `rc` to pin "
        f"the newest published stack ({REPIN_ENV}=1 also repins the DFE images "
        "at their newest GitHub releases)",
    )
    args = parser.parse_args()
    requested = (args.version or "").strip()
    try:
        if not requested:
            raise StackError(
                "no VERSION given -- usage: make stack VERSION=X.Y.Z[-rc.N]|latest"
            )
        discover = requested in DISCOVERY_WORDS
        repin = discover and _repin_requested()
        version = (
            latest_tag(prereleases=DISCOVERY_WORDS[requested], repo=manifest_repo())
            if discover
            else requested
        )
        _ensure_env()
        fragment, source = _resolve_fragment(version)
        pins = _parse_pins(fragment)
        if not pins:
            raise StackError(
                f"stack render for {version!r} produced no *_VERSION pins "
                f"(source: {source})"
            )
        if repin:
            pins |= _latest_image_pins(prereleases=DISCOVERY_WORDS[requested])
        _refuse_undigested(pins)
        # Record WHICH stack these pins came from, alongside them. `make modes`
        # reports this key and `VERSION ?= $(DFE_STACK_VERSION)` defaults from
        # it, so a stale value makes both describe a stack the box is not on.
        # A discovered set records the WORD, so a re-run refreshes rather than
        # freezing on the stack that happened to be newest at the time.
        resolved = f"  # resolved {version}" + (" + newest DFE images" if repin else "")
        marker = {
            STACK_VERSION_KEY: f"{STACK_VERSION_KEY}={requested}"
            + (resolved if discover else "")
        }
        merged = _merge_into_env(
            DOTENV_FILE.read_text(encoding="utf-8", errors="replace"), pins | marker
        )
        DOTENV_FILE.write_text(merged, encoding="utf-8", newline="\n")
        _print(
            msg=(
                f"Pinned {len(pins)} image version(s) for stack {version} into "
                f"{_rel_path(path=DOTENV_FILE)} (source: {source})"
            )
        )
        for key in pins:
            _print(msg=f"  {pins[key]}")
        stamped = marker[STACK_VERSION_KEY]
        _print(
            msg=f"  {stamped}"
            if discover
            else f"  {stamped}  # the stack these came from"
        )
        if repin:
            _print(
                msg=(
                    "This set is NEWER than any certified stack -- for development "
                    "and integration, not a deployment. Pin a version to deploy."
                )
            )
        elif discover:
            _print(
                msg=(
                    f"`{requested}` follows the newest published stack on every "
                    "re-run -- pin a version to deploy."
                )
            )
    except (RegistryError, StackError) as error:
        _print(msg=f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
