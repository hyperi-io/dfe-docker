#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/env_files.py
#  Purpose:      Gate the start targets on the per-service env files, creating any that are missing
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Guard `make dev` / `make ci` on env/<service>.env, and heal what it can.

A release that adds a new env.example/<service>.env would otherwise fail every
already-initialised deployment -- the unattended updater included -- until a
human ran `make init`. Creating the file is exactly what `make init` does, and it
is non-destructive by contract (existing files are skipped, secrets only topped
up), so this runs it and re-checks rather than refusing. A file still missing
afterwards is a real fault and exits non-zero.

The file-level check cannot see a template that grew a KEY inside a file that
already exists, so the key-level drift `init.py` computes is reported here as a
warning. It stays a warning: an operator's env file is theirs, and this gate
starts stacks rather than editing their config.

Silent when nothing is missing and nothing has drifted.
"""

from __future__ import annotations

from _common import (
    DOTENV_FILE,
    DOTENV_TEMPLATE,
    ENV_DIR,
    ENV_TEMPLATE_DIR,
    _print,
    _rel_path,
)
from init import drift_keys
from init import main as init_main


def _missing_files() -> list[str]:
    """Return the names of every env.example template with no env/ counterpart."""
    return sorted(
        template.name
        for template in ENV_TEMPLATE_DIR.glob("*.env")
        if not ((ENV_DIR / template.name).is_file())
    )


def _report_drift(*, include_dotenv: bool) -> None:
    """Warn about template keys that never reached the file they belong in.

    `include_dotenv` is off once init.py has run, because it reports .env drift
    itself and a second copy of the same list reads as two findings.
    """
    pairs = [(DOTENV_FILE, DOTENV_TEMPLATE)] if include_dotenv else []
    pairs += [
        (ENV_DIR / template.name, template)
        for template in sorted(ENV_TEMPLATE_DIR.glob("*.env"))
    ]
    for target, template in pairs:
        if not (target.is_file()) or not (template.is_file()):
            continue
        missing = drift_keys(dotenv_path=target, template_path=template)
        if not (missing):
            continue
        _print(
            header=_rel_path(path=target),
            msg=f"WARNING: {len(missing)} key(s) in {_rel_path(path=template)} are not "
            f"in this file -- review and copy across if wanted: {', '.join(missing)}",
        )


def grant_group_write() -> None:
    """Let the operator's group write env/, which is how dfe-engine writes there.

    The engine runs as a UID of its own and joins the operator's GID
    (`group_add` in docker-compose.yml) to write each app's custom env file.
    """
    if not ENV_DIR.is_dir():
        return
    mode = ENV_DIR.stat().st_mode
    # Only when a bit is missing: chmod on a directory another user owns raises.
    if mode & 0o070 != 0o070:
        ENV_DIR.chmod(mode | 0o070)


def main() -> int:
    missing = _missing_files()
    if missing:
        _print(msg=f"env/ is missing {', '.join(missing)} -- running `make init`")
        status = init_main()
        still_missing = _missing_files()
        if still_missing:
            _print(
                msg=f"env/ is still missing {', '.join(still_missing)} after "
                f"scripts/init.py (exit {status}) -- the start targets need every "
                f"file in {_rel_path(path=ENV_TEMPLATE_DIR)}/"
            )
            return 1
    grant_group_write()
    _report_drift(include_dotenv=not missing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
