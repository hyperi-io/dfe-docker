#  Project:      dfe-docker
#  File:         tests/test_e2e_posture.py
#  Purpose:      Assert `make e2e-posture` writes the suite's stack and refuses a deployment
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The e2e posture overwrites the admin password, so it shares `make dev`'s refusal."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import creds
import e2e_posture
from _common import COMPOSE_FILE

_MINTED = "s3cr3t-minted-value"
_NO_POSTURE = f"DFE_AUTH_LOCAL_ADMIN_PASSWORD={_MINTED}\nDFE_UI_PORT=3000\n"


def _backups(directory: Path) -> list[Path]:
    return sorted(directory.glob(".env.bak-*"))


def test_a_deployment_is_refused_and_left_alone(dotenv: Path) -> None:
    text = f"DFE_ENV=production\n{_NO_POSTURE}"
    dotenv.write_text(text, encoding="utf-8", newline="\n")

    assert e2e_posture.main() == 2
    assert dotenv.read_text(encoding="utf-8") == text
    assert _backups(dotenv.parent) == []


def test_the_posture_is_written_in_place_and_backed_up(dotenv: Path) -> None:
    dotenv.write_text(_NO_POSTURE, encoding="utf-8", newline="\n")

    assert e2e_posture.main() == 0

    written = dotenv.read_text(encoding="utf-8")
    shipped = creds._DEFAULT_PASSWORD
    assert "DFE_ENV=test\n" in written
    assert "DFE_E2E_SERVER=true\n" in written
    assert "DFE_CONTAINER_PREFIX=e2e-\n" in written
    assert f"DFE_AUTH_LOCAL_ADMIN_PASSWORD={shipped}\n" in written
    assert _MINTED not in written
    assert "DFE_UI_PORT=23000\n" in written
    assert written.count("DFE_UI_PORT=") == 1
    # A fixed project name would attach this stack to another's volumes.
    assert "COMPOSE_PROJECT_NAME" not in written
    assert len(_backups(dotenv.parent)) == 1


def test_a_second_run_changes_nothing(dotenv: Path) -> None:
    dotenv.write_text(_NO_POSTURE, encoding="utf-8", newline="\n")
    assert e2e_posture.main() == 0
    settled = dotenv.read_text(encoding="utf-8")

    assert e2e_posture.main() == 0

    assert dotenv.read_text(encoding="utf-8") == settled
    assert len(_backups(dotenv.parent)) == 1


def test_every_published_host_port_is_moved() -> None:
    published = set(
        re.findall(r"\$\{([A-Z_]+_PORT):-", COMPOSE_FILE.read_text(encoding="utf-8"))
    )
    # In-network as well as host ports: their host side has variables of its own.
    in_network = {"CLICKHOUSE_HTTP_PORT", "CLICKHOUSE_NATIVE_PORT"}
    # Commented-out ingest publishes nothing starts on.
    unpublished = {
        "DFE_RECEIVER_BEATS_PORT",
        "DFE_RECEIVER_HEC_PORT",
        "DFE_RECEIVER_OTLP_GRPC_PORT",
        "DFE_RECEIVER_OTLP_HTTP_PORT",
    }

    assert published - in_network - unpublished <= set(e2e_posture.PORTS)
    assert len(set(e2e_posture.PORTS.values())) == len(e2e_posture.PORTS)


@pytest.mark.parametrize("key", ["DFE_ENV", "DFE_E2E_SERVER"])
def test_the_engine_is_handed_what_the_posture_writes(key: str) -> None:
    assert f"      {key}: ${{{key}:-" in COMPOSE_FILE.read_text(encoding="utf-8")
