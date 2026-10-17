#  Project:      dfe-docker
#  File:         tests/test_version_check_dial.py
#  Purpose:      Assert the release-check dial reaches every app that phones out, defaulting on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One .env line opts every DFE app out of the startup release check.

Each app reads `version_check.*` under its own scalo cascade prefix, so the
compose file maps the shared dial onto each service's own spelling. This reads
docker-compose.yml as text, which needs neither docker nor a resolved stack.
`make check-compose` resolves the same keys through `docker compose config`.
"""

import re

import pytest

import check_compose
from _common import COMPOSE_FILE
from test_engine_only_schema_control import _blocks

ENABLED = "${VERSION_CHECK__ENABLED:-true}"
SEND_INSTANCE_ID = "${VERSION_CHECK__SEND_INSTANCE_ID:-true}"
# The only forms the dial may take in the compose file: it opts out and never defaults off.
_DIAL_VALUE = re.compile(r"^\$\{VERSION_CHECK__(ENABLED|SEND_INSTANCE_ID):-true\}$")


def _environment(block: list[str]) -> dict[str, str]:
    """One service's `environment:` mapping as written, key to raw value."""
    found: dict[str, str] = {}
    inside = False
    for line in block:
        if line.startswith("    environment:"):
            inside = True
            continue
        stripped = line.strip()
        if not inside or not stripped or stripped.startswith("#"):
            continue
        if not line.startswith("      "):
            break
        key, _, value = stripped.partition(": ")
        found[key] = value
    return found


def _services() -> dict[str, dict[str, str]]:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    return {name: _environment(block) for name, block in _blocks(text).items()}


@pytest.mark.parametrize(
    ("service", "prefix"), sorted(check_compose._VERSION_CHECK_PREFIXES.items())
)
def test_each_app_maps_the_dial_onto_its_own_prefix(service: str, prefix: str) -> None:
    environment = _services()[service]

    assert environment.get(f"{prefix}VERSION_CHECK__ENABLED") == ENABLED
    assert (
        environment.get(f"{prefix}VERSION_CHECK__SEND_INSTANCE_ID") == SEND_INSTANCE_ID
    )


def test_the_dial_defaults_on_wherever_it_appears() -> None:
    """A default of false would opt every deployment out without being asked."""
    values = [
        (service, key, value)
        for service, environment in _services().items()
        for key, value in environment.items()
        if "VERSION_CHECK__" in key
    ]

    assert values
    assert [v for v in values if not _DIAL_VALUE.match(v[2])] == []


def test_no_service_outside_the_list_carries_the_dial() -> None:
    """dfe-ui and the hunt runner start no check, so a key there reads nothing."""
    carriers = {
        service
        for service, environment in _services().items()
        if any("VERSION_CHECK__" in key for key in environment)
    }

    assert carriers == set(check_compose._VERSION_CHECK_PREFIXES)
