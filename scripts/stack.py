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

Transport is LOCAL-PATH-FIRST, OCI-FALLBACK:

1. A local dfe-infra checkout (env ``DFE_INFRA_DIR``, default the sibling
   ``../dfe-infra``): run ``scripts/dfe-stack render --target docker --stack
   VERSION`` and read its stdout fragment.
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
    DOTENV_FILE,
    DOTENV_TEMPLATE,
    PROJECTS_PATH,
    _print,
    _rel_path,
)

# `make stack` depends on `.env` existing, and creating one is init's job -- it is
# the only place that mints the generated secrets. A plain copy here would produce
# a .env whose secrets are still the sentinel placeholders, which starts but fails
# `make post`.
from init import _create_dotenv

# Default sibling directory holding a dfe-infra checkout (the stack SSoT repo).
DEFAULT_INFRA_DIRNAME = "dfe-infra"
# OCI repository for the signed stack-manifest artifact (air-gap / CI path).
DEFAULT_MANIFEST_REPO = "ghcr.io/hyperi-io/dfe-stack-manifest"

# A rendered pin line: KEY=value[  # annotation]. Keys are UPPER_SNAKE ending in
# _VERSION; only such active (non-comment) lines from the fragment are merged.
_PIN_LINE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<rest>.+)$")


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

    ``DFE_INFRA_DIR`` overrides the default sibling ``../dfe-infra``. An
    explicitly-set but rendererless directory is an error (loud, not silent);
    an absent default simply falls through to the OCI path.
    """
    raw = os.environ.get("DFE_INFRA_DIR", "").strip()
    candidate = (
        Path(raw).expanduser() if raw else (PROJECTS_PATH / DEFAULT_INFRA_DIRNAME)
    )
    if (candidate / "scripts" / "dfe-stack").is_file():
        return candidate
    if raw:
        raise StackError(
            f"DFE_INFRA_DIR={raw!r} has no scripts/dfe-stack renderer -- "
            "point it at a dfe-infra checkout or unset it to use the OCI path"
        )
    return None


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
    repo = (
        os.environ.get("DFE_STACK_MANIFEST_REPO", "").strip() or DEFAULT_MANIFEST_REPO
    )
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
    repo = (
        os.environ.get("DFE_STACK_MANIFEST_REPO", "").strip() or DEFAULT_MANIFEST_REPO
    )
    return _render_oci(version), f"OCI stack manifest ({repo}:{version})"


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
        "version", nargs="?", help="stack version to pin, e.g. 2.2.0 or 2.2.0-rc.1"
    )
    args = parser.parse_args()
    version = (args.version or "").strip()
    try:
        if not version:
            raise StackError(
                "no VERSION given -- usage: make stack VERSION=X.Y.Z[-rc.N]"
            )
        _ensure_env()
        fragment, source = _resolve_fragment(version)
        pins = _parse_pins(fragment)
        if not pins:
            raise StackError(
                f"stack render for {version!r} produced no *_VERSION pins "
                f"(source: {source})"
            )
        merged = _merge_into_env(
            DOTENV_FILE.read_text(encoding="utf-8", errors="replace"), pins
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
    except StackError as error:
        _print(msg=f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
