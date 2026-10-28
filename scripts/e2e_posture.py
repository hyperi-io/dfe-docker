#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         e2e_posture.py
#  Purpose:      Put .env into the posture the dfe-ui Playwright suite runs against, and back
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Switch .env into the posture the dfe-ui Playwright suite needs, and out again.

    python3 scripts/e2e_posture.py on       # make e2e-posture
    python3 scripts/e2e_posture.py off      # make e2e-posture-off
    python3 scripts/e2e_posture.py check    # make e2e-posture-check

The suite seeds its fixtures through the engine's /api/e2e routes, which the engine
mounts only with DFE_E2E_SERVER on, and whose seeders refuse anything but
DFE_ENV=test. `on` sets both, plus the shipped admin password the suite's reset returns
the account to, and refuses on a .env whose DFE_ENV names a deployment, exit 2.

The .env it replaces is copied to `.env.pre-e2e` (0600) the first time, and `off`
puts that copy back and removes it, so a second `on` never loses the original.
`check` exits 1 naming what is missing, so the suite can refuse before forty specs fail.
The engine reads .env at start, so a running stack needs recreating after either switch.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import (  # noqa: E402
    DOTENV_FILE,
    _dotenv_values,
    _print,
    _rel_path,
    write_dotenv,
    write_private,
)
from creds import _ADMIN_PASSWORD_KEY, _DEFAULT_PASSWORD, is_dev_posture  # noqa: E402
from dev_posture import _rewrite  # noqa: E402

_POSTURE_KEY = "DFE_ENV"
_SERVER_KEY = "DFE_E2E_SERVER"
_E2E_ENV = "test"
_REFUSED = 2
_BANNER = "## Written by `make e2e-posture` - the dfe-ui Playwright suite's stack, not a deployment."

WANTED = {
    _POSTURE_KEY: _E2E_ENV,
    _ADMIN_PASSWORD_KEY: _DEFAULT_PASSWORD,
    _SERVER_KEY: "true",
}


def _saved() -> Path:
    return DOTENV_FILE.with_name(f"{DOTENV_FILE.name}.pre-e2e")


def _missing(*, values: dict[str, str]) -> list[str]:
    """What the suite needs that .env does not say."""
    missing = []
    if values.get(_POSTURE_KEY, "").strip() != _E2E_ENV:
        missing.append(f"{_POSTURE_KEY}={_E2E_ENV}")
    if values.get(_SERVER_KEY, "").strip().lower() != "true":
        missing.append(f"{_SERVER_KEY}=true")
    return missing


def _switch_on() -> int:
    values = _dotenv_values()
    environment = values.get(_POSTURE_KEY, "").strip()
    if environment and environment != _E2E_ENV and not is_dev_posture(environment):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg=f"DFE_ENV={environment} is not a dev posture, so `make e2e-posture` "
            "refuses: it would overwrite this deployment's admin password with the "
            "shipped default. Run the suite from a checkout of its own.",
        )
        return _REFUSED

    text = DOTENV_FILE.read_text(encoding="utf-8")
    rewritten = _rewrite(banner=_BANNER, text=text, wanted=WANTED)
    if rewritten == text:
        _print(header=_rel_path(path=DOTENV_FILE), msg="E2E posture already set")
        return 0

    if not _saved().exists():
        write_private(path=_saved(), text=text)
    write_dotenv(path=DOTENV_FILE, text=rewritten)
    _print(
        header=_rel_path(path=DOTENV_FILE),
        msg="E2E posture: DFE_ENV=test, DFE_E2E_SERVER=true, admin password the shipped "
        f"default. `make e2e-posture-off` restores {_rel_path(path=_saved())}. "
        "Recreate the stack so the engine reads it.",
    )
    return 0


def _switch_off() -> int:
    if not _saved().is_file():
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg="Nothing to restore: `make e2e-posture` has not changed this .env",
        )
        return 0
    write_dotenv(path=DOTENV_FILE, text=_saved().read_text(encoding="utf-8"))
    _saved().unlink()
    _print(
        header=_rel_path(path=DOTENV_FILE),
        msg="Restored the .env from before `make e2e-posture`. Recreate the stack so the "
        "engine reads it.",
    )
    return 0


def _check() -> int:
    missing = _missing(values=_dotenv_values())
    if missing:
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg=f"Not in the e2e posture: {', '.join(missing)} missing. The dfe-ui "
            "Playwright suite needs the engine's /api/e2e routes. Run "
            "`make e2e-posture`, then recreate the stack.",
        )
        return 1
    return 0


def main(*, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("on", "off", "check"))
    args = parser.parse_args(args=argv)

    if not DOTENV_FILE.is_file():
        _print(
            header=_rel_path(path=DOTENV_FILE), msg="Missing -- run `make init` first"
        )
        return 1
    return {"on": _switch_on, "off": _switch_off, "check": _check}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
