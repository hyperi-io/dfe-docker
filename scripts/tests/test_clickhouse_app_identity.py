#  Project:      dfe-docker
#  File:         tests/test_clickhouse_app_identity.py
#  Purpose:      Fail the build when an app service takes the ClickHouse admin
#                credential. Only dfe-engine connects as it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The guard for one ClickHouse user per app.

dfe-engine creates every ClickHouse user, so it alone connects as the admin
account (CLICKHOUSE_USERNAME and CLICKHOUSE_PASSWORD). Each app connects as the
user the engine creates for it, on a password `make init` mints, and the engine is
handed the same password to create that user on. dfe-infra's
docs/CLICKHOUSE-IDENTITY.md is the model, and its Kubernetes guard is
scripts/tests/test_clickhouse_app_identity.py there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import init
import post
from test_engine_only_schema_control import _blocks

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"

# The server itself, and the one client that administers it.
ADMIN_HOLDERS = {"clickhouse", "dfe-engine"}

# Each app's user, the .env key its password is minted under, and the service
# whose definition carries the pair.
APP_USERS = {
    "dfe_hunt_runner": ("DFE_HUNT_RUNNER_CLICKHOUSE_PASSWORD", "dfe-hunt-runner"),
    "dfe_loader": ("DFE_LOADER_CLICKHOUSE_PASSWORD", "dfe-loader"),
    "dfe_otel_writer": ("DFE_OTEL_WRITER_CLICKHOUSE_PASSWORD", "otel-collector"),
    "dfe_query_reader": ("DFE_QUERY_READER_CLICKHOUSE_PASSWORD", "hyperdx"),
}

# The variable the engine adopts each app's password from.
ENGINE_READS = {
    "dfe_hunt_runner": "DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD",
    "dfe_loader": "DFE_CLICKHOUSE_LOADER_PASSWORD",
    "dfe_otel_writer": "DFE_CLICKHOUSE_OTEL_WRITER_PASSWORD",
    "dfe_query_reader": "DFE_CLICKHOUSE_QUERY_READER_PASSWORD",
}


@pytest.fixture(scope="module")
def services() -> dict[str, str]:
    blocks = {
        name: "\n".join(lines)
        for name, lines in _blocks(COMPOSE.read_text(encoding="utf-8")).items()
    }
    assert len(blocks) > 10, f"the compose reader found only {sorted(blocks)}"
    return blocks


def test_only_the_engine_takes_the_admin_password(services: dict[str, str]) -> None:
    holders = {
        name for name, block in services.items() if "${CLICKHOUSE_PASSWORD" in block
    }
    assert holders == ADMIN_HOLDERS


def test_only_the_engine_takes_the_admin_user(services: dict[str, str]) -> None:
    holders = {
        name for name, block in services.items() if "${CLICKHOUSE_USERNAME" in block
    }
    assert holders <= ADMIN_HOLDERS
    assert "dfe-engine" in holders


@pytest.mark.parametrize("user", sorted(APP_USERS))
def test_each_app_connects_as_its_own_user(services: dict[str, str], user: str) -> None:
    key, service = APP_USERS[user]
    block = services[service]
    assert user in block, f"{service} names no {user}"
    assert f"${{{key}:-}}" in block, f"{service} does not connect with {key}"


@pytest.mark.parametrize("user", sorted(APP_USERS))
def test_the_engine_is_handed_each_app_password(
    services: dict[str, str], user: str
) -> None:
    key, _service = APP_USERS[user]
    assert f"{ENGINE_READS[user]}: ${{{key}:-}}" in services["dfe-engine"]


@pytest.mark.parametrize("user", sorted(APP_USERS))
def test_each_app_password_is_minted_and_enforced(user: str) -> None:
    key, service = APP_USERS[user]
    assert key in init.GENERATED_SECRETS
    assert post.WEAK_SECRET_DEFAULTS[key] == ("", service)
