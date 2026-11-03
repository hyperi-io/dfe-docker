#  Project:      dfe-docker
#  File:         tests/test_install_sh.py
#  Purpose:      Assert install.sh verifies, unpacks and configures a bundle, and refuses what it must, with docker stubbed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""install.sh run for real against a small bundle served from a file:// URL, with a stub `docker` on PATH that records what it was asked to start."""

import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from _common import REPO_ROOT

INSTALL = REPO_ROOT / "install.sh"
SECRETS = (
    ".env CLICKHOUSE_PASSWORD alnum 24\n"
    ".env DFE_API_JWT_SECRET alnum 48\n"
    "engine.env DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD alnum 24\n"
    "hyperdx.env TOKEN_ENCRYPTION_KEY hex 64\n"
)
FILES = {
    ".env.example": "# CLICKHOUSE_PASSWORD=\n# DFE_API_JWT_SECRET=changeme\nDFE_UI_PORT=3000\n",
    "env.example/engine.env": "# DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD=\n",
    "env.example/hyperdx.env": "# TOKEN_ENCRYPTION_KEY=\n",
    "install/secrets.list": SECRETS,
    "install/stack.env": "DFE_ENGINE_VERSION=v1@sha256:" + "c" * 64 + "\n",
    "install/profiles/single-kafka.env": "DFE_OTEL_RESOLVED=true\nDFE_KAFKA_BOOTSTRAP_SERVERS=kafka:9092\n",
    "install/profiles/single-kafka.conf": 'PROFILES="clickhouse,kafka-redpanda"\nSERVICES="dfe-loader dfe-engine"\n',
    "install/profiles/slim-grpc.env": "DFE_OTEL_RESOLVED=false\n",
    "install/profiles/slim-grpc.conf": 'PROFILES="clickhouse"\nSERVICES="dfe-loader"\n',
    "docker-compose.yml": "services: {}\n",
}
STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
exit 0
"""


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """A release directory laid out as GitHub serves `latest`: SHA256SUMS and the bundle."""
    root = tmp_path / "release" / "latest" / "download"
    root.mkdir(parents=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in FILES.items():
            data = text.encode()
            info = tarfile.TarInfo(f"dfe/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    bundle = buffer.getvalue()
    (root / "dfe-docker-9.9.9.tar.gz").write_bytes(bundle)
    (root / "SHA256SUMS").write_text(
        f"{hashlib.sha256(bundle).hexdigest()}  dfe-docker-9.9.9.tar.gz\n",
        encoding="utf-8",
    )
    return tmp_path / "release"


def _run(
    tmp_path: Path, release: Path, **extra: str
) -> tuple[subprocess.CompletedProcess, Path, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    docker = stubs / "docker"
    docker.write_text(STUB, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    environ = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "DFE_RELEASE_URL": f"file://{release}",
        "DFE_DIR": str(tmp_path / "dfe"),
        **extra,
    }
    result = subprocess.run(
        ["sh", str(INSTALL)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=environ,
        errors="replace",
        text=True,
    )
    return result, tmp_path / "dfe", log.read_text(encoding="utf-8")


def test_installs_the_default_profile_with_minted_secrets(
    tmp_path: Path, release: Path
) -> None:
    result, target, calls = _run(tmp_path, release)

    assert result.returncode == 0, result.stderr
    env = (target / ".env").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1)
        for line in env.splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert re.fullmatch(r"[A-Za-z0-9]{24}", values["CLICKHOUSE_PASSWORD"])
    assert re.fullmatch(r"[A-Za-z0-9]{48}", values["DFE_API_JWT_SECRET"])
    assert values["DFE_PROFILE"] == "single-kafka"
    assert values["COMPOSE_FILE"] == "docker-compose.yml:compose/container-logs.yml"
    assert values["COMPOSE_PROFILES"] == "clickhouse,kafka-redpanda"
    assert values["DFE_ENGINE_VERSION"].startswith("v1@sha256:")
    assert re.fullmatch(
        r"DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD=[A-Za-z0-9]{24}",
        (target / "env" / "engine.env").read_text(encoding="utf-8").strip(),
    )
    assert re.fullmatch(
        r"TOKEN_ENCRYPTION_KEY=[0-9a-f]{64}",
        (target / "env" / "hyperdx.env").read_text(encoding="utf-8").strip(),
    )
    assert "compose version" in calls
    assert "compose up -d --wait --wait-timeout 900 dfe-loader dfe-engine" in calls
    assert "docker compose --profile '*' down -v" in result.stdout
    assert not (target / ".env.new").exists()


def test_a_second_run_gets_other_secrets(tmp_path: Path, release: Path) -> None:
    first = _run(tmp_path, release)[1] / ".env"
    first_text = first.read_text(encoding="utf-8")
    first.parent.rename(tmp_path / "dfe-first")

    second = _run(tmp_path, release)[1] / ".env"

    assert second.read_text(encoding="utf-8") != first_text


def test_a_profile_is_chosen_with_dfe_profile(tmp_path: Path, release: Path) -> None:
    result, target, calls = _run(tmp_path, release, DFE_PROFILE="slim-grpc")

    assert result.returncode == 0, result.stderr
    assert "COMPOSE_FILE=docker-compose.yml\n" in (target / ".env").read_text(
        encoding="utf-8"
    )
    assert "compose up -d --wait --wait-timeout 900 dfe-loader\n" in calls


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        pytest.param(
            {"DFE_PROFILE": "single-grpc"},
            "Choose one of: single-kafka slim-grpc",
            id="unknown_profile",
        ),
    ],
)
def test_refuses_what_it_cannot_install(
    tmp_path: Path, release: Path, extra: dict, message: str
) -> None:
    result, target, calls = _run(tmp_path, release, **extra)

    assert result.returncode == 1
    assert message in result.stderr
    assert "up -d" not in calls
    assert not target.exists()


def test_refuses_a_bundle_whose_sha256_does_not_match(
    tmp_path: Path, release: Path
) -> None:
    sums = release / "latest" / "download" / "SHA256SUMS"
    sums.write_text("0" * 64 + "  dfe-docker-9.9.9.tar.gz\n", encoding="utf-8")

    result, target, calls = _run(tmp_path, release)

    assert result.returncode == 1
    assert "does not match its sha256" in result.stderr
    assert not (target / ".env").exists()
    assert "up -d" not in calls


def test_refuses_to_overwrite_a_directory_that_exists(
    tmp_path: Path, release: Path
) -> None:
    (tmp_path / "dfe").mkdir()

    result, _, calls = _run(tmp_path, release)

    assert result.returncode == 1
    assert "exists" in result.stderr
    assert "up -d" not in calls


def test_names_what_is_missing_in_one_line(tmp_path: Path, release: Path) -> None:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    result = subprocess.run(
        [shutil.which("sh") or "/bin/sh", str(INSTALL)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={"PATH": str(stubs), "DFE_RELEASE_URL": f"file://{release}"},
        text=True,
    )

    assert result.returncode == 1
    assert (
        result.stderr.strip()
        == "install.sh: needs curl or wget to download the release, and neither is installed"
    )
