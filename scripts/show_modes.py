#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/show_modes.py
#  Purpose:      State the two deploy-currency modes and which one this box uses
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""State the deploy-currency contract: pinned, track-latest, or latest.

A dfe-docker checkout stays current one of three mutually exclusive ways, and
which one a box is on should never have to be guessed:

  PINNED (default, production-safe) -- `make stack VERSION=X.Y.Z && make ci`
    pins the whole certified set from the signed stack-manifest and starts it.
    The stack hard-fails rather than ever pull `latest`. `make dial` sets the
    pin from the deployment dial's version.pin, so a dial edit picks the version.

  TRACK-LATEST (opt-in daemon) -- `ops/daemon-update/install.sh` installs a
    systemd timer that DISCOVERS the newest certified stack tag and runs the
    SAME `make stack` + `make ci`, but only when something newer has shipped.
    It never pulls `latest`; -rc builds join in with DFE_UPDATE_ALLOW_PRERELEASE=1.

  LATEST (development only) -- `make stack VERSION=latest` takes the newest
    certified stack and then repins every DFE image at its own newest published
    tag, which is a combination nobody certified. Still digest-pinned, so it is
    reproducible; it is just not a set anyone tested together.

This reads local files only (the deployment dial and .env), so it is safe on a
fresh checkout and offline. The live "what is the newest published stack" answer
needs the registry and stays in ops/daemon-update/self_update.py --dry-run, which
this points at rather than duplicates.
"""

from __future__ import annotations

from _common import (
    DEPLOYMENT_DIAL,
    REPO_ROOT,
    _dotenv_values,
    _parse_yaml_subset,
)
from _registry import DISCOVERY_WORDS

# self_update.py records the last-applied version here (its STATE_FILENAME): a
# track-latest box grows one, a purely pinned box never does. Kept in step with
# ops/daemon-update/self_update.py -- fold onto a shared constant if a third
# consumer appears.
_APPLIED_STATE = REPO_ROOT / ".dfe-stack-applied"


def _dial_scalar(dial: dict[str, object], path: tuple[str, ...]) -> str | None:
    """Return the non-empty scalar at ``path`` in the parsed dial, else None."""
    node: object = dial
    for step in path:
        if not (isinstance(node, dict)):
            return None
        node = node.get(step)
    return node.strip() if isinstance(node, str) and node.strip() else None


def _read_dial() -> dict[str, object]:
    """Parse the deployment dial if present, else an empty dial (fresh checkout)."""
    if not (DEPLOYMENT_DIAL.is_file()):
        return {}
    return _parse_yaml_subset(
        text=DEPLOYMENT_DIAL.read_text(encoding="utf-8", errors="replace")
    )


def _applied_version() -> str:
    """Return the version the track-latest daemon last applied, or '' if none."""
    if not (_APPLIED_STATE.is_file()):
        return ""
    return _APPLIED_STATE.read_text(encoding="utf-8", errors="replace").strip()


def _current_mode() -> tuple[str, str]:
    """Return (label, detail) for the mode THIS checkout is wired for.

    Read from local state only: the dial's version.{pin,track} is the intent, and
    .env's DFE_STACK_VERSION is what `make stack`/`make dial` actually wrote. The
    two version dials are mutually exclusive, so both set is a misconfiguration
    worth naming rather than silently resolving.
    """
    dial = _read_dial()
    track = _dial_scalar(dial, ("version", "track"))
    pin = _dial_scalar(dial, ("version", "pin"))
    env_pin = _dotenv_values().get("DFE_STACK_VERSION", "").strip()
    applied = _applied_version()

    if track and (pin or env_pin):
        return (
            "AMBIGUOUS",
            (
                "the dial sets BOTH version.track and a pin -- comment one out; they "
                "are mutually exclusive (the daemon would re-pin over `make stack`)"
            ),
        )
    if track:
        return (
            "TRACK-LATEST",
            f"daemon owns the version (last applied {applied})"
            if applied
            else "daemon owns the version (nothing applied yet)",
        )
    # `make stack VERSION=latest|rc` stamps the WORD, not a number, because a
    # re-run is meant to refresh. Reporting that as PINNED would claim a
    # reproducible deployment the box does not have.
    if env_pin in DISCOVERY_WORDS:
        return (
            "LATEST (unpinned DFE images)",
            f"`make stack VERSION={env_pin}` -- newer than any certified stack; "
            "development and integration only",
        )
    if pin or env_pin:
        return "PINNED", f"at {env_pin or pin}"
    return (
        "UNCONFIGURED",
        "no pin and no track set -- run `make dial` or pass VERSION= to `make stack`",
    )


def main() -> int:
    print("dfe-docker deploy modes -- three ways to stay current (mutually exclusive):")
    print()
    print("  PINNED (default, production-safe)")
    print("    make stack VERSION=X.Y.Z && make ci")
    print("    Pins the whole certified set from the signed stack-manifest, then")
    print("    starts it. Hard-fails rather than pull `latest`. `make dial` sets")
    print("    the pin from deployment.yaml's version.pin.")
    print()
    print("  TRACK-LATEST (opt-in daemon)")
    print("    sudo ops/daemon-update/install.sh   # installs the systemd timer")
    print("    Discovers the newest certified stack tag and runs the same")
    print("    `make stack` + `make ci`, only when something newer ships. Never")
    print("    pulls `latest`. -rc builds included with DFE_UPDATE_ALLOW_PRERELEASE=1.")
    print()
    print("  LATEST (development only, NOT a deployment)")
    print("    make stack VERSION=latest   # `rc` to rank pre-releases throughout")
    print("    Takes the newest certified stack, then repins every DFE image at")
    print("    its own newest published tag -- a combination nobody certified.")
    print("    Still digest-pinned; re-run it to move forward.")
    print()

    label, detail = _current_mode()
    print(f"this checkout: {label} -- {detail}")
    print("live latest-vs-applied: python3 ops/daemon-update/self_update.py --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
