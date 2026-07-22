#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         init.py
#  Purpose:      Create .env and per-service env/<service>.env files from templates
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Bootstrap local config files from their templates.

Copies .env.example to .env and each env.example/<service>.env to env/<service>.env. Existing files are left untouched (reported as skipped) so the target is safe to re-run. Errors go to stderr with a non-zero exit.

Two things happen beyond a plain copy:

Generated secrets. The keys in GENERATED_SECRETS have deterministic, committed defaults in docker-compose.yml, which means a stack that never ran this script is using a signing key and a database password anyone with the repo already knows. This script mints a random value for each - into a new .env, and topped up into an existing .env that predates the key. Compose cannot hard-fail on them (its interpolation is not profile-gated, so a `:?` would abort `make down` too, for services the operator may not even run), so the enforcement lives in the power-on self test: scripts/post.py refuses to pass while a default is still in place.

Drift report. A re-run reports template keys that never reached .env. The copy is one-shot, so an operator who ran `make init` months ago otherwise never learns that .env.example grew a setting. Reporting is all it does - editing an operator's .env is theirs to do, not ours.
"""

from __future__ import annotations

import re
import secrets
import shutil
import string
from pathlib import Path

from _common import (
    DOTENV_FILE,
    DOTENV_TEMPLATE,
    ENV_DIR,
    ENV_TEMPLATE_DIR,
    _print,
    _rel_path,
)

# Secrets that must not be left at their weak/empty default. scripts/post.py
# enforces them: DFE_UI_NEXTAUTH_SECRET and HYPERDX_POSTGRES_PASSWORD via its
# WEAK_SECRET_DEFAULTS service-map, CLICKHOUSE_PASSWORD via its own external-CH
# check (its default is empty, not a sentinel string). Keep the three in step.
#
# CLICKHOUSE_PASSWORD is a BREAKING change on upgrade: a ClickHouse data volume
# created with the old empty password does not re-authenticate against a generated
# one. docs/operating.md documents the migration + recovery. `make init` only
# tops up an existing .env that predates the key, so an operator who already set a
# password keeps it.
# HyperI password standard (hyperi-ai standards/universal/security.md): generated
# secrets are ALPHANUMERIC ONLY -- `token_urlsafe` emits `-`/`_`, which are shell
# metacharacters that truncate a password in a URL-form DSN, and which the policy
# forbids. Length is the entropy control, not the charset: over a 62-symbol
# alphabet (5.95 bits/char), 24 chars is ~143 bits (128-bit tier: service and DB
# credentials) and 48 is ~286 bits (256-bit tier: long-lived key-protecting
# secrets). The 24/48 split is fixed; the surrounding policy is WIP.
_SECRET_ALPHABET = string.ascii_letters + string.digits
_LEN_CREDENTIAL = 24  # DB / service credentials
_LEN_KEY = 48  # signing keys and other long-lived key material

# Name -> length tier. DB passwords are the 128-bit credential tier; the dfe-ui
# NextAuth value is a session-signing KEY, so it takes the 256-bit tier.
GENERATED_SECRETS = {
    "CLICKHOUSE_PASSWORD": _LEN_CREDENTIAL,
    "HYPERDX_POSTGRES_PASSWORD": _LEN_CREDENTIAL,
    "DFE_UI_NEXTAUTH_SECRET": _LEN_KEY,
}

# Matches a dotenv assignment: live (`KEY=value`) or a single-hash commented-out
# setting (`# KEY=value`).
#
# A SINGLE hash only, deliberately. This file's convention is `#` for a
# commented-out setting and `##` for prose, and the prose genuinely contains lines
# like "## DFE_BIND_HOST=0.0.0.0. Do that knowingly..." mid-sentence. A looser
# `#*` matched those, which is harmless for drift reporting but not for
# _render_secrets: the day anyone writes `## DFE_UI_NEXTAUTH_SECRET=...` in a
# comment explaining the key, that documentation line would be replaced by a live
# random assignment.
_SETTING_RE = re.compile(r"^[ \t]*(?:#[ \t]?)?(?P<key>[A-Z][A-Z0-9_]*)[ \t]*=")


def _generate_secret(length: int) -> str:
    """Return a fresh alphanumeric random secret of `length` chars (no punctuation)."""
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(length))


def _setting_key(*, line: str) -> str | None:
    """Return the dotenv key a line assigns, commented or not - None if it assigns nothing."""
    match = _SETTING_RE.match(line)
    return match.group("key") if match else None


def _live_keys(*, text: str) -> set[str]:
    """Return the keys a dotenv file gives a real VALUE to.

    A commented line does not count, and neither does a bare ``KEY=``. Compose's
    ``${VAR:?}`` fires on unset OR empty, so treating an empty assignment as "set"
    would leave `make init` unable to fix the very case its own error message
    tells the operator to run it for.
    """
    live = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        key = _setting_key(line=line)
        if key and line.partition("=")[2].strip().strip('"').strip("'"):
            live.add(key)
    return live


def _all_keys(*, text: str) -> set[str]:
    """Return every key a dotenv file mentions, live or commented."""
    return {key for line in text.splitlines() if (key := _setting_key(line=line))}


def _render_secrets(*, text: str) -> str:
    """Replace each GENERATED_SECRETS placeholder line with a live, randomly generated assignment."""
    pending = set(GENERATED_SECRETS)
    rendered = []
    for line in text.splitlines():
        key = _setting_key(line=line)
        if key in pending:
            pending.discard(key)
            rendered.append(f"{key}={_generate_secret(GENERATED_SECRETS[key])}")
            continue
        rendered.append(line)

    # A key the template never mentioned still has to exist, or the stack runs on
    # a committed default.
    if pending:
        rendered.append("")
        rendered.append("## Generated by `make init` - keep out of version control.")
        rendered.extend(
            f"{key}={_generate_secret(GENERATED_SECRETS[key])}"
            for key in sorted(pending)
        )
    return "\n".join(rendered) + "\n"


def _create_dotenv(*, dst_path: Path, src_path: Path) -> None:
    """Write a new .env from its template with generated secrets materialised."""
    template = src_path.read_text(encoding="utf-8")
    with dst_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(_render_secrets(text=template))
    _print(
        header=_rel_path(path=dst_path),
        msg=f"Created (generated {len(GENERATED_SECRETS)} secrets)",
    )


def _top_up_secrets(*, dotenv_path: Path) -> None:
    """Give every GENERATED_SECRETS key a real value in an existing .env.

    Two cases, because they need different repairs. A key the file never mentions
    is appended. A key present but EMPTY (`KEY=`) is rewritten in place - appending
    a second assignment would work, since the last one wins, but leaving a
    duplicate key in an operator's .env is how the next person loses an hour.
    """
    text = dotenv_path.read_text(encoding="utf-8")
    missing = sorted(set(GENERATED_SECRETS) - _live_keys(text=text))
    if not (missing):
        return

    pending = set(missing)
    rewritten = []
    for line in text.splitlines():
        key = _setting_key(line=line)
        if key in pending and not (line.lstrip().startswith("#")):
            pending.discard(key)
            rewritten.append(f"{key}={_generate_secret(GENERATED_SECRETS[key])}")
            continue
        rewritten.append(line)

    if pending:
        rewritten.append("")
        rewritten.append(
            "## Generated by `make init` - the power-on self test fails without these."
        )
        rewritten.extend(
            f"{key}={_generate_secret(GENERATED_SECRETS[key])}"
            for key in sorted(pending)
        )

    with dotenv_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(rewritten) + "\n")
    _print(
        header=_rel_path(path=dotenv_path),
        msg=f"Generated missing secrets: {', '.join(missing)}",
    )


def _report_drift(*, dotenv_path: Path, template_path: Path) -> None:
    """Report template keys absent from an existing .env - the copy is one-shot, so it drifts."""
    template_keys = _all_keys(text=template_path.read_text(encoding="utf-8"))
    dotenv_keys = _all_keys(text=dotenv_path.read_text(encoding="utf-8"))
    missing = sorted(template_keys - dotenv_keys)
    if not (missing):
        return
    _print(
        header=_rel_path(path=dotenv_path),
        msg=f"{len(missing)} setting(s) added to {_rel_path(path=template_path)} since this file was created - review and copy across if wanted:\n"
        + "\n".join(f"  - {key}" for key in missing),
    )


def _copy_if_absent(*, dst_path: Path, src_path: Path) -> None:
    """Copy src_path to dst_path unless dst_path already exists - report the outcome."""
    if dst_path.exists():
        _print(header=_rel_path(path=dst_path), msg="Skipped (already exists)")
        return
    shutil.copy(dst=dst_path, src=src_path)
    _print(header=_rel_path(path=dst_path), msg="Created")


def main() -> int:
    if not (DOTENV_TEMPLATE.exists()):
        _print(
            header=_rel_path(path=DOTENV_TEMPLATE),
            msg="Missing template",
        )
        return 1

    if DOTENV_FILE.exists():
        _print(header=_rel_path(path=DOTENV_FILE), msg="Skipped (already exists)")
        _top_up_secrets(dotenv_path=DOTENV_FILE)
        _report_drift(dotenv_path=DOTENV_FILE, template_path=DOTENV_TEMPLATE)
    else:
        _create_dotenv(dst_path=DOTENV_FILE, src_path=DOTENV_TEMPLATE)

    ENV_DIR.mkdir(exist_ok=True, parents=True)

    templates = sorted(ENV_TEMPLATE_DIR.glob("*.env"))
    if not (templates):
        template_dir = f"{_rel_path(path=ENV_TEMPLATE_DIR)}/"
        _print(
            header=template_dir,
            msg="No component *.env templates",
        )
        return 1

    for src in templates:
        _copy_if_absent(dst_path=ENV_DIR / src.name, src_path=src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
