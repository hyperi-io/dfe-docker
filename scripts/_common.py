#  Project:      dfe-docker
#  File:         _common.py
#  Purpose:      Single source of truth for the repo resources.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Shared repo layout for the scripts/tooling.

Internal support module - imported by the runnable scripts, not executed directly. Resolves the repo root once and exposes the well-known file/dir locations the scripts read and write so every script agrees on where things live. Add further shared locations here, alphabetical by name.
"""

from __future__ import annotations

import os
import re
import sys
import typing
from pathlib import Path

# File to mark the root of the repository
_REPO_MARKER = "docker-compose.yml"


def __find_repo_root(*, marker: str = _REPO_MARKER) -> Path:
    """Return the nearest ancestor (inclusive) of this module containing marker."""
    current = Path(__file__).resolve()
    for candidate in (current, *current.parents):
        if (candidate / marker).exists():
            return candidate
    raise SystemExit(f"Could not locate repo root (no {marker!r} in any parent)")


# Basic constants
FALSY = {"", "0", "false", "no", "off"}

# Repo constants
REPO_ROOT = __find_repo_root()
CONFIG_DIR = REPO_ROOT / "config"
DEPLOYMENT_DIAL = REPO_ROOT / "deployment.yaml"
DEPLOYMENT_DIAL_TEMPLATE = REPO_ROOT / "deployment.example.yaml"
DOTENV_FILE = REPO_ROOT / ".env"
DOTENV_TEMPLATE = REPO_ROOT / ".env.example"
ENV_DIR = REPO_ROOT / "env"
ENV_TEMPLATE_DIR = REPO_ROOT / "env.example"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
COMPOSE_LIVE_FILE = REPO_ROOT / "docker-compose.live.yml"
# Generated per run by `make dev LOCAL=...`, never committed.
COMPOSE_LOCAL_FILE = REPO_ROOT / "docker-compose.local.yml"
COMPOSE_OVERRIDE_FILE = REPO_ROOT / "docker-compose.override.yml"
PROFILE_MK = REPO_ROOT / ".profile.mk"
RUST_BUILDER = REPO_ROOT / "docker" / "dfe-rust-builder.Dockerfile"
SERVICE_PROFILES_FILE = REPO_ROOT / "service_profiles.yaml"

# `${VAR:?message}` -- the form that makes an unset value abort the command
# instead of resolving to something silently wrong. `${VAR:-default}` is optional
# by construction and needs nothing from us.
_REQUIRED_VAR_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")


def _print(
    *, file: typing.TextIO | None = sys.stderr, header: str | None = None, msg: str
) -> None:
    """Print a custom message with caller script name prefixed."""
    print(
        f"{Path(sys.argv[0]).stem}{f' ({header})' if header else ''}: {msg}", file=file
    )


def _rel_path(*, path: Path) -> str:
    """Return path relative to the repo root for display."""
    return str(path.relative_to(REPO_ROOT))


def _config_topics(*, path: Path) -> tuple[set[str], set[str], str]:
    """Return (subscribed topics, produced topics, subscription regex) of one service config.

    A deliberately small reader rather than a YAML parse: the service configs use
    three topic keys and nothing else, and the callers have to run with no PyYAML
    (CI installs it for ``make test-e2e`` only). A config that derives its topics
    from a ``dfe_source`` instead of naming them returns empty sets.
    """
    subscribed: set[str] = set()
    produced: set[str] = set()
    regex = ""
    in_list = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not (line) or line.startswith("#"):
            continue
        if in_list and line.startswith("- "):
            subscribed.add(line[2:].strip().strip("\"'"))
            continue
        in_list = line == "topics:"
        if line.startswith("topic: "):
            produced.add(line.split(": ", 1)[1].strip().strip("\"'"))
        elif line.startswith("topic_regex: "):
            regex = line.split(": ", 1)[1].strip().strip("\"'")
    return subscribed, produced, regex


def _config_enrichment_paths(*, path: Path) -> set[str]:
    """Return the container paths a service config's `enrichment_tables` entries name.

    The same deliberately small reader as `_config_topics`, for the same reason:
    the callers run with no PyYAML. A config with no enrichment tables returns an
    empty set.
    """
    paths: set[str] = set()
    in_block = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not (line) or line.startswith("#"):
            continue
        if line == "enrichment_tables:":
            in_block = True
            continue
        if in_block and not (raw.startswith((" ", "\t"))):
            in_block = False
        if in_block and (line.startswith("path: ") or line.startswith("- path: ")):
            paths.add(line.split("path: ", 1)[1].strip().strip("\"'"))
    return paths


def _dotenv_values() -> dict[str, str]:
    """Parse .env into a dict, honouring the same minimal subset docker compose does.

    Commented and blank lines are skipped; an unquoted value has a trailing ` #`
    comment stripped, and surrounding quotes are removed. Returns empty if there
    is no .env - a fresh checkout is a normal state, not an error.
    """
    if not (DOTENV_FILE.is_file()):
        return {}
    values: dict[str, str] = {}
    for raw in DOTENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not (line) or (line.startswith("#")) or ("=" not in line):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value and value[0] not in ("'", '"'):
            value = value.split(" #", 1)[0].strip()
        values[key] = value.strip('"').strip("'")
    return values


def _load_dotenv() -> None:
    """Merge .env into os.environ. Existing environment variables take precedence."""
    for key, value in _dotenv_values().items():
        os.environ.setdefault(key, value)


def _parse_yaml_subset(*, text: str) -> dict[str, object]:
    """Parse a minimal YAML subset - nested maps, scalar string values - into dicts.

    Dependency-free (no PyYAML): the scripts run under a plain ``python3`` on the
    devex VMs. Handles ``key: value`` scalars and ``key:`` nesting by indentation,
    skipping ``#`` comments and blank lines. It does NOT handle lists or inline
    collections - a line it cannot place raises ValueError. Values come back as
    strings (quotes stripped), which is all a dotenv render needs.

    resolve_profile.py keeps a local twin of this for service_profiles.yaml; fold
    that onto this shared one when convenient.
    """
    root: dict[str, object] = {}
    stack: list[tuple[dict[str, object], int]] = [(root, -1)]
    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not (line) or line.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        while len(stack) > 1 and stack[-1][1] >= indent:
            stack.pop()
        parent = stack[-1][0]
        if ": " in line:
            key, value = line.split(": ", 1)
            value = value.strip()
            # Strip a trailing ` # ...` inline comment on an UNQUOTED value, the
            # same way _dotenv_values does - a quoted value keeps its `#` literal.
            if value and value[0] not in ("'", '"'):
                value = value.split(" #", 1)[0].strip()
            parent[key.strip()] = value.strip('"').strip("'")
        elif line.endswith(":"):
            child: dict[str, object] = {}
            parent[line[:-1].strip()] = child
            stack.append((child, indent))
        else:
            raise ValueError(f"line {line_num}: cannot parse {line!r}")
    return root


def _profile_mk_value(*, key: str) -> list[str]:
    """Return the whitespace-split value of one `.profile.mk` assignment.

    `.profile.mk` is regenerated by `make` from service_profiles.yaml, so it is
    the resolved answer to "what is this stack meant to be". Empty list when the
    profile has not been resolved yet or the key is absent.
    """
    if not (PROFILE_MK.is_file()):
        return []
    for line in PROFILE_MK.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        if name.replace("export", "").strip() == key:
            return value.split()
    return []


def _resolved_services() -> list[str]:
    """Return the services the ACTIVE profile says should be running.

    This is the intended stack, which is not the same as "whatever answers on a
    port". Stray containers from a previous profile, an older checkout whose
    `make down` was still profile-scoped, or something started by hand will all
    answer a probe while being wired to a pipeline that is not running. Anything
    deciding what to talk to needs the profile, not a port probe.
    """
    return _profile_mk_value(key="DFE_SERVICES")


def _resolved_profiles() -> list[str]:
    """Return the COMPOSE profiles the active service profile turns on.

    Note this is the infrastructure half only -- `PROFILE_FLAGS` carries
    clickhouse/kafka/kafka-ui, while the DFE services are named explicitly on the
    `up` command line rather than selected by profile. Anything reasoning about
    the whole stack needs this AND `_resolved_services()`.
    """
    return [p for p in _profile_mk_value(key="PROFILE_FLAGS") if p != "--profile"]


def _required_compose_vars(*, files: tuple[Path, ...] = (COMPOSE_FILE,)) -> set[str]:
    """Return every variable the given compose files declare mandatory via ``${VAR:?...}``.

    Discovered from the compose files rather than listed anywhere, so adding a new
    hard-fail key cannot silently escape the checks that depend on this. The
    default covers docker-compose.yml alone -- the image-pin surface -- which is
    what the pin-focused consumers (check_hardfail, show_limits) want.
    """
    found: set[str] = set()
    for path in files:
        found |= set(_REQUIRED_VAR_RE.findall(path.read_text(encoding="utf-8")))
    return found
