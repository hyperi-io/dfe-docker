#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         e2e_posture.py
#  Purpose:      Put .env and env/engine.env into the posture the dfe-ui Playwright suite runs against, and back
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Switch .env and env/engine.env into the dfe-ui Playwright suite's posture (`on`), back (`off`) or report what is missing (`check`)."""

import argparse
import sys
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import (  # noqa: E402
    DOTENV_FILE,
    ENGINE_COMPONENT,
    _dotenv_values,
    _env_file,
    _env_file_values,
    _print,
    _rel_path,
    write_dotenv,
    write_private,
)
from creds import _ADMIN_PASSWORD_KEY, _DEFAULT_PASSWORD, is_dev_posture  # noqa: E402
from dev_posture import _rewrite  # noqa: E402

_BANNER = "## Written by `make e2e-posture` - the Playwright suite's stack"
_E2E_ENV = "test"
_POSTURE_KEY = "DFE_ENV"
# Distinct from 1 (no env files at all) so a caller can tell a deployment from a checkout that never ran make init
_REFUSED = 2
_SERVER_KEY = "DFE_E2E_SERVER"


def _check() -> int:
    """Exit 1 naming what the suite needs that the env files do not say."""
    missing = _missing()
    if missing:
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg=f"Not in the e2e posture: {', '.join(missing)} missing. The dfe-ui Playwright suite needs the engine's /api/e2e routes. Run `make e2e-posture`, then recreate the stack.",
        )
        return 1
    return 0


def _missing() -> list[str]:
    """What the suite needs that .env and env/engine.env do not say."""
    missing = []
    if _dotenv_values().get(_POSTURE_KEY, "").strip() != _E2E_ENV:
        missing.append(f"{_POSTURE_KEY}={_E2E_ENV}")
    server = _env_file_values(component=ENGINE_COMPONENT).get(_SERVER_KEY, "")
    if server.strip().lower() != "true":
        missing.append(f"{_SERVER_KEY}=true")
    return missing


def _saved(*, path: Path) -> Path:
    """The copy `on` keeps of a file it rewrote."""
    return path.with_name(f"{path.name}.pre-e2e")


def _switch_off() -> int:
    """Put back each file `on` rewrote and remove its copy."""
    restored = []
    for path in _wanted():
        saved = _saved(path=path)
        if not (saved.is_file()):
            continue
        write_dotenv(path=path, text=saved.read_text(encoding="utf-8"))
        saved.unlink()
        restored.append(_rel_path(path=path))
    if not (restored):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg="Nothing to restore: `make e2e-posture` has not changed this checkout",
        )
        return 0
    _print(
        header=_rel_path(path=DOTENV_FILE),
        msg=f"Restored {', '.join(restored)} from before `make e2e-posture`. Recreate the stack so the engine reads them.",
    )
    return 0


def _switch_on() -> int:
    """Write the posture into both files, keeping each original the first time."""
    environment = _dotenv_values().get(_POSTURE_KEY, "").strip()
    if environment and environment != _E2E_ENV and not (is_dev_posture(environment)):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg=f"DFE_ENV={environment!r} is not a dev posture, so `make e2e-posture` refuses: it would overwrite this deployment's admin password with the shipped default. Run the suite from a checkout of its own.",
        )
        return _REFUSED

    changes = []
    for path, keys in _wanted().items():
        text = path.read_text(encoding="utf-8")
        rewritten = _rewrite(banner=_BANNER, text=text, wanted=keys)
        if rewritten != text:
            changes.append((path, text, rewritten))
    if not (changes):
        _print(header=_rel_path(path=DOTENV_FILE), msg="E2E posture already set")
        return 0

    for path, text, rewritten in changes:
        if not (_saved(path=path).exists()):
            write_private(path=_saved(path=path), text=text)
        write_dotenv(path=path, text=rewritten)
    _print(
        header=_rel_path(path=DOTENV_FILE),
        msg="E2E posture: DFE_ENV=test in .env, DFE_E2E_SERVER=true and the shipped admin password in env/engine.env. `make e2e-posture-off` restores both. Recreate the stack so the engine reads them.",
    )
    return 0


def _wanted() -> dict[Path, dict[str, str]]:
    """The keys `on` writes, by the file that holds them."""
    return {
        DOTENV_FILE: {_POSTURE_KEY: _E2E_ENV},
        _env_file(component=ENGINE_COMPONENT): {
            _ADMIN_PASSWORD_KEY: _DEFAULT_PASSWORD,
            _SERVER_KEY: "true",
        },
    }


def main(*, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "off", "on"))
    args = parser.parse_args(args=argv)

    for path in _wanted():
        if not (path.is_file()):
            _print(header=_rel_path(path=path), msg="Missing, run `make init` first")
            return 1
    actions = {"check": _check, "off": _switch_off, "on": _switch_on}
    return actions[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
