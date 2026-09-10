#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/render_profiles.py
#  Purpose:      Project the Kubernetes slim and single profiles into
#                service_profiles.yaml, and check the committed block matches.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Project the Kubernetes slim and single tiers into service_profiles.yaml.

`slim` and `single` here are the compose renderings of the Kubernetes tiers of
the same name, and Kubernetes is the master: the shape lives in dfe-infra's
``argocd/values/profile-<mode>.yaml`` and this tool renders it down. Every other
profile in service_profiles.yaml is a hand-crafted data-plane shape with no
Kubernetes counterpart, and nothing here touches them.

    make render-profiles      # rewrite the projected block
    make check-profiles       # exit 1 if the committed block is stale

The renderer is dfe-infra's ``scripts/dfe-stack render --docker-profile``, so
the mapping from Kubernetes values to compose keys is stated once, over there,
beside the profile files it reads. Point ``DFE_INFRA_DIR`` at a dfe-infra
checkout -- the same explicit-local-only rule ``make stack`` uses, so nothing
here probes for a sibling directory it was not told about.

The block is delimited by the BEGIN/END markers below. Everything outside them
is left byte-for-byte alone.
"""

from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
from pathlib import Path

from _common import SERVICE_PROFILES_FILE, _print, _rel_path

# The profiles that are projections. Order is the order they are written in.
PROJECTED = ("slim", "single")

BEGIN = "  # BEGIN projected profiles -- rendered by `make render-profiles`\n"
END = "  # END projected profiles\n"
# Inside the block, so an editor reads why their change will be overwritten.
BANNER = (
    "  # Kubernetes is the master for these two: edit dfe-infra\n"
    "  # argocd/values/profile-<mode>.yaml, then re-render. See docs/profiles.md.\n"
)


class RenderError(Exception):
    """Raised when the projection cannot be rendered or the block is missing."""


def _infra_dir() -> Path:
    """The dfe-infra checkout holding the renderer, from DFE_INFRA_DIR only."""
    raw = os.environ.get("DFE_INFRA_DIR", "").strip()
    if not raw:
        raise RenderError(
            "DFE_INFRA_DIR is unset -- point it at a dfe-infra checkout, which is "
            "where the profile shapes live"
        )
    candidate = Path(raw).expanduser()
    if not (candidate / "scripts" / "dfe-stack").is_file():
        raise RenderError(
            f"DFE_INFRA_DIR={raw!r} has no scripts/dfe-stack renderer -- point it "
            "at a dfe-infra checkout"
        )
    return candidate


def _render(infra_dir: Path, mode: str) -> str:
    out = subprocess.run(
        [
            sys.executable,
            str(infra_dir / "scripts" / "dfe-stack"),
            "render",
            "--docker-profile",
            mode,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        detail = out.stderr.strip() or out.stdout.strip()
        raise RenderError(f"rendering {mode} failed: {detail}")
    return out.stdout


def projected_block(infra_dir: Path) -> str:
    """The whole delimited block, markers included."""
    body = "\n".join(_render(infra_dir, mode).rstrip("\n") for mode in PROJECTED)
    return f"{BEGIN}{BANNER}{body}\n{END}"


def _split(text: str) -> tuple[str, str, str]:
    """(before, block, after) around the markers."""
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 or end < 0 or end < start:
        raise RenderError(
            f"{_rel_path(path=SERVICE_PROFILES_FILE)} has no projected-profile "
            "markers -- restore them around the slim and single profiles"
        )
    return text[:start], text[start : end + len(END)], text[end + len(END) :]


def _diff(current: str, fresh: str) -> list[str]:
    """The stale block against the fresh one, for a CI log that names the drift."""
    return list(
        difflib.unified_diff(
            current.splitlines(),
            fresh.splitlines(),
            fromfile="committed",
            tofile="rendered",
            lineterm="",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="render_profiles.py", description=__doc__.split("\n")[0].strip()
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit 1 without writing (for CI)",
    )
    args = parser.parse_args()
    try:
        text = SERVICE_PROFILES_FILE.read_text(encoding="utf-8", errors="replace")
        before, current, after = _split(text)
        fresh = projected_block(_infra_dir())
        if current == fresh:
            _print(msg="projected profiles are up to date")
            return 0
        if args.check:
            _print(
                msg=(
                    f"{_rel_path(path=SERVICE_PROFILES_FILE)} is STALE against the "
                    "Kubernetes profiles -- run `make render-profiles`"
                )
            )
            for line in _diff(current, fresh):
                print(line, file=sys.stderr)
            return 1
        SERVICE_PROFILES_FILE.write_text(
            before + fresh + after, encoding="utf-8", newline="\n"
        )
        _print(
            msg=f"rewrote the projected profiles in {_rel_path(path=SERVICE_PROFILES_FILE)}"
        )
    except RenderError as error:
        _print(msg=f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
