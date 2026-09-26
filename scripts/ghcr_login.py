#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/ghcr_login.py
#  Purpose:      Authenticate docker + oras to the image registry from .env
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Authenticate the local docker daemon (and oras) to the GHCR image registry.

`make ci` runs `docker compose pull` and `make stack` runs `oras pull` against
the private `ghcr.io/hyperi-io/dfe-*` images and the signed stack-manifest. Both
assume the daemon is already logged in to the registry. On an operator laptop
that is a one-off `docker login`; on an unattended VM (the daemon-update timer,
a config-management run) there is no human to run it. This target closes
that gap: it reads the registry credentials and logs both docker and oras in, so
the pull just works with nothing typed.

Credential source (environment WINS over .env, so a caller -- the daemon timer,
an ansible role -- can inject without editing the file):

    DFE_GHCR_USERNAME   the GHCR account / bot user
    DFE_GHCR_TOKEN      a PAT (or GITHUB_TOKEN) with read:packages

The registry HOST is derived from IMAGE_REGISTRY (default ghcr.io/hyperi-io ->
ghcr.io); a registry login is per-host, not per-repo. When the credentials are
absent this is a deliberate NO-OP (exit 0) with a stated reason: a daemon already
authenticated out of band (an existing ~/.docker/config.json) must not be forced
to carry a token in .env, and the eventual pull surfaces a clear auth error if it
genuinely is not logged in.

The token is passed to `docker login` / `oras login` on STDIN
(--password-stdin) and is never echoed, never placed on a command line, and never
written anywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from _common import _dotenv_values, _print

DEFAULT_REGISTRY = "ghcr.io/hyperi-io"
USERNAME_KEY = "DFE_GHCR_USERNAME"
TOKEN_KEY = "DFE_GHCR_TOKEN"


def _value(key: str, dotenv: dict[str, str]) -> str:
    """Return a value from the environment (wins) or .env, stripped."""
    return (os.environ.get(key) or dotenv.get(key, "")).strip()


def _registry_host(dotenv: dict[str, str]) -> str:
    """The registry HOST to log in to, from IMAGE_REGISTRY (repo path stripped)."""
    registry = _value("IMAGE_REGISTRY", dotenv) or DEFAULT_REGISTRY
    return registry.split("/", 1)[0]


def _login(tool: str, host: str, username: str, token: str) -> None:
    """Run `<tool> login <host> -u <username> --password-stdin`; token via stdin only."""
    result = subprocess.run(
        [tool, "login", host, "-u", username, "--password-stdin"],
        input=token,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SystemExit(f"ghcr_login: `{tool} login {host}` failed: {detail}")
    _print(msg=f"{tool} authenticated to {host} as {username}")


def main() -> int:
    dotenv = _dotenv_values()
    username = _value(USERNAME_KEY, dotenv)
    token = _value(TOKEN_KEY, dotenv)
    host = _registry_host(dotenv)

    if not username or not token:
        _print(
            msg=(
                f"{USERNAME_KEY}/{TOKEN_KEY} not set -- skipping registry login "
                f"(assuming the daemon is already authenticated to {host}). "
                "Set both in .env, or the environment, for an unattended pull."
            )
        )
        return 0

    if shutil.which("docker") is None:
        raise SystemExit("ghcr_login: docker is not installed")
    _login("docker", host, username, token)

    # oras is only needed by `make stack` (the signed OCI stack-manifest). Skip
    # it gracefully when absent rather than fail a plain `make ci`.
    if shutil.which("oras") is not None:
        _login("oras", host, username, token)
    else:
        _print(msg="oras not installed -- skipped (only `make stack` needs it)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
