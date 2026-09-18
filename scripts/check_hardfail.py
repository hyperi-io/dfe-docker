#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/check_hardfail.py
#  Purpose:      Assert an unpinned stack refuses to start instead of pulling `latest`
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Assert that a fresh, unpinned checkout hard-fails.

Compose is a supported production deploy target, so "which image am I running?"
should not have a silent answer. Nearly every image pin uses the
``${VAR:?message}`` form, which makes an unset value abort the command with an
actionable message rather than resolve to ``latest``.

The generated SECRETS are deliberately not in that set -- Compose interpolates
every service before profiles filter anything, so a ``:?`` on a secret would abort
``make down`` for an operator who never enabled the service it belongs to. They
carry a loud sentinel default and ``make post`` fails on it instead.

That is a property, not a line of code, and properties rot quietly: one
``${VAR:-latest}`` slipped in during a refactor and the guarantee is gone with
nothing failing. So this asserts it directly -- run compose with NOTHING set and
require that it fails, and that it fails for the right reason.

Run deliberately hostile to itself: an empty ``--env-file`` so a developer's
pinned ``.env`` cannot mask the check, and a scrubbed environment so exported
shell vars cannot either. On CI neither exists, but the check must mean the same
thing in both places or it is not worth running.

Scope, stated plainly so a green run is not read as more than it earns: this
proves the stack refuses to resolve with NOTHING set. It does NOT prove every
image is pinned. Compose aborts on the FIRST missing variable, so one surviving
``${VAR:?}`` anywhere is enough to make this pass -- a new ``${SOMETHING:-latest}``
slipping in alongside it would not be caught.

The second check covers what the first cannot see. Compose aborts on the FIRST
missing variable, so one surviving ``${VAR:?}`` is enough to make the run above
pass, and a new ``${SOMETHING:-latest}`` beside it would resolve to whatever the
registry serves. So every ``image:`` is read directly and required to resolve to a
DIGEST: a mandatory ``${VAR:?}`` whose value `make stack` refuses to write without
one, or a default that carries the digest itself.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

from _common import COMPOSE_FILE, REPO_ROOT, _print, _required_compose_vars

# What compose says when a `${VAR:?}` is unset. Matching on this rather than the
# exit code alone distinguishes "hard-failed as designed" from "broken YAML".
_EXPECTED = "is missing a value"

_IMAGE_LINE = re.compile(r"^\s*image:\s*(?P<ref>\S.*?)\s*$")
# `${NAME:-default}` or `${NAME:?message}`. Defaults carry no braces of their own.
_INTERPOLATION = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<form>:[-?])(?P<default>[^}]*)\}"
)
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")


def _undigested_images() -> list[str]:
    """Return a `file:line: reference` for every image that can resolve undigested.

    The LAST interpolation on the line is the tag -- the first, where there is
    one, is the registry prefix.
    """
    findings: list[str] = []
    text = COMPOSE_FILE.read_text(encoding="utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        match = _IMAGE_LINE.match(line)
        if not match:
            continue
        reference = match.group("ref")
        interpolations = list(_INTERPOLATION.finditer(reference))
        tag = interpolations[-1] if interpolations else None
        if tag is not None and tag.group("form") == ":?":
            continue
        carrier = tag.group("default") if tag is not None else reference
        if _DIGEST.search(carrier):
            continue
        findings.append(f"{COMPOSE_FILE.name}:{number}: {reference}")
    return findings


def _scrubbed_env() -> dict[str, str]:
    """Return the environment with every mandatory key removed."""
    env = dict(os.environ)
    for name in _required_compose_vars():
        env.pop(name, None)
    return env


def main() -> int:
    required = _required_compose_vars()
    if not (required):
        _print(
            msg="No `${VAR:?}` hard-fail keys found in docker-compose.yml -- the "
            "fresh-checkout guarantee has been lost"
        )
        return 1

    with tempfile.NamedTemporaryFile(prefix="dfe-empty-env-", suffix=".env") as empty:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                empty.name,
                "-f",
                COMPOSE_FILE.name,
                "config",
                "-q",
            ],
            capture_output=True,
            cwd=REPO_ROOT,
            encoding="utf-8",
            env=_scrubbed_env(),
            errors="replace",
            text=True,
        )

    if result.returncode == 0:
        _print(
            msg=f"An unpinned stack RESOLVED successfully. {len(required)} key(s) are "
            "declared mandatory but compose did not enforce them -- a fresh "
            "checkout would silently run unpinned images"
        )
        return 1

    detail = (result.stderr or result.stdout).strip()
    if _EXPECTED not in detail:
        _print(
            msg=f"Compose failed, but not with a hard-fail on a missing pin:\n{detail}"
        )
        return 1

    _print(msg=f"OK   unpinned stack hard-fails ({len(required)} mandatory key(s))")
    _print(msg=f"     {detail.splitlines()[0]}")

    undigested = _undigested_images()
    if undigested:
        _print(
            msg=f"{len(undigested)} image(s) can resolve without a digest -- give the "
            "tag a mandatory `${VAR:?}` the stack pins, or a default carrying "
            "`@sha256:`"
        )
        for finding in undigested:
            _print(msg=f"     {finding}")
        return 1

    _print(msg="OK   every image resolves to a digest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
