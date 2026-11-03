#  Project:      dfe-docker
#  File:         tests/test_apply_sh.py
#  Purpose:      Assert apply.sh turns the settings in .env into the compose chain the Makefile builds, with docker stubbed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""apply.sh run for real in a small install directory, with a stub `docker` on PATH that answers `docker info` and records the rest."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import proxy_tls
from _common import REPO_ROOT

APPLY = REPO_ROOT / "apply.sh"
LINKS = '[{"name":"Kafbat","url":"http://localhost:8081"}]'
PROFILE_ENV = f"DFE_OTEL_RESOLVED=true\nDFE_ADMIN_LINKS_RESOLVED='{LINKS}'\n"
STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case $1 in
    info) echo "8 testhost" ;;
    network) [ -n "${NET_SUBNETS:-}" ] || exit 1; echo "$NET_SUBNETS " ;;
esac
exit 0
"""
needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl mints the console certificate"
)


def _install(
    tmp_path: Path, *, settings: str = "", profile_env: str = PROFILE_ENV
) -> Path:
    """An install directory as install.sh leaves it, before apply.sh has run."""
    target = tmp_path / "dfe"
    profiles = target / "install" / "profiles"
    profiles.mkdir(parents=True)
    (target / ".env").write_text(
        f"DFE_PROFILE=single-kafka\n{settings}", encoding="utf-8"
    )
    (target / "apply.sh").write_text(
        APPLY.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (profiles / "single-kafka.env").write_text(profile_env, encoding="utf-8")
    (profiles / "single-kafka.conf").write_text(
        'PROFILES="clickhouse,kafka-redpanda"\nSERVICES="dfe-loader dfe-engine"\n',
        encoding="utf-8",
    )
    (profiles / "single-kafka.usage").write_text(
        '{"clickhouse":2.0,"dfe-loader":-1.0}\n', encoding="utf-8"
    )
    for variant in ("kafbat", "hyperdx", "kafbat-hyperdx"):
        (profiles / f"single-kafka.{variant}.env").write_text(
            profile_env.replace(LINKS, "[]"), encoding="utf-8"
        )
    return target


def _apply(
    tmp_path: Path, target: Path, *args: str, net_subnets: str = ""
) -> tuple[subprocess.CompletedProcess, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    docker = stubs / "docker"
    docker.write_text(STUB, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["sh", "apply.sh", *args],
        capture_output=True,
        check=False,
        cwd=target,
        encoding="utf-8",
        env={
            **os.environ,
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "DOCKER_LOG": str(log),
            "NET_SUBNETS": net_subnets,
        },
        errors="replace",
        text=True,
    )
    return result, log.read_text(encoding="utf-8")


def _values(target: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in (target / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )


def _fragment(target: Path, name: str) -> list[str]:
    return (target / ".tmp" / name).read_text(encoding="utf-8").splitlines()[1:]


def test_the_defaults_chain_the_log_driver_and_fill_in_the_hosts_cores(
    tmp_path: Path,
) -> None:
    target = _install(tmp_path)

    result, calls = _apply(tmp_path, target)

    assert result.returncode == 0, result.stderr
    values = _values(target)
    assert values["COMPOSE_FILE"] == "docker-compose.yml:compose/container-logs.yml"
    assert values["COMPOSE_PROFILES"] == "clickhouse,kafka-redpanda"
    assert values["DFE_UI_BIND_HOST"] == "127.0.0.1"
    assert values["DFE_USAGE_CPU_LIMITS"] == '\'{"clickhouse":2.0,"dfe-loader":8.0}\''
    assert values["DFE_USAGE_NODE_NAME"] == "testhost"
    assert "compose up -d --wait --wait-timeout 900 dfe-loader dfe-engine" in calls
    assert not (target / ".tmp" / "docker-compose.unpublish.yml").exists()


def test_a_rerun_replaces_its_block_and_keeps_every_other_line(tmp_path: Path) -> None:
    target = _install(tmp_path, settings="DFE_UI_PORT=3100\n")
    _apply(tmp_path, target, "--no-start")
    with (target / ".env").open("a", encoding="utf-8") as env:
        env.write("DFE_DATA_ROOT=" + str(tmp_path / "data") + "\n")

    result, calls = _apply(tmp_path, target, "--no-start")

    assert result.returncode == 0, result.stderr
    text = (target / ".env").read_text(encoding="utf-8")
    assert text.count("## BEGIN apply.sh") == 1
    assert text.count("COMPOSE_FILE=") == 1
    assert "DFE_UI_PORT=3100" in text
    assert _values(target)["COMPOSE_FILE"].startswith(
        "docker-compose.yml:compose/storage.yml"
    )
    assert "up" not in calls


def test_a_data_root_chains_the_storage_overlay_and_makes_its_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    target = _install(tmp_path, settings=f"DFE_DATA_ROOT={root}\n")

    result, _ = _apply(tmp_path, target, "--no-start")

    assert result.returncode == 0, result.stderr
    assert _values(target)["COMPOSE_FILE"].split(":")[:2] == [
        "docker-compose.yml",
        "compose/storage.yml",
    ]
    for volume in (
        "clickhouse",
        "kafka-redpanda",
        "engine-config",
        "hyperdx-documentdb",
    ):
        assert (root / volume).is_dir()


@pytest.mark.parametrize(
    ("settings", "unpublished", "embed", "links"),
    [
        ("DFE_UI_EXTERNAL=false", ["dfe-proxy"], False, LINKS),
        ("DFE_ENGINE_API_EXTERNAL=false", ["dfe-engine"], False, LINKS),
        ("DFE_KAFBAT_UI_EXTERNAL=false", ["kafka-ui"], False, "[]"),
        (
            "DFE_HYPERDX_UI_EXTERNAL=false",
            ["dfe-hyperdx-proxy"],
            True,
            "[]",
        ),
        (
            "DFE_INFRA_UIS_EXTERNAL=false",
            ["kafka-ui", "dfe-hyperdx-proxy"],
            True,
            "[]",
        ),
        (
            "DFE_INFRA_UIS_EXTERNAL=false\nDFE_UI_EXTERNAL=false",
            ["dfe-proxy", "kafka-ui", "dfe-hyperdx-proxy"],
            False,
            "[]",
        ),
    ],
)
def test_an_exposure_flag_takes_its_ports_off_the_host(
    tmp_path: Path,
    settings: str,
    unpublished: list[str],
    embed: bool,
    links: str,
) -> None:
    target = _install(tmp_path, settings=f"{settings}\n")

    result, _ = _apply(tmp_path, target, "--no-start")

    assert result.returncode == 0, result.stderr
    assert ":.tmp/docker-compose.unpublish.yml" in _values(target)["COMPOSE_FILE"]
    lines = _fragment(target, "docker-compose.unpublish.yml")
    assert [
        line.strip(" :")
        for line in lines
        if line.startswith("  ") and line.endswith(":")
    ][: len(unpublished)] == unpublished
    assert ("ports: !override" in "".join(line.strip() for line in lines)) is embed
    assert f"DFE_ADMIN_LINKS_RESOLVED='{links}'" in (target / ".env").read_text(
        encoding="utf-8"
    )


def test_the_unpublish_fragment_matches_the_one_the_makefile_writes(
    tmp_path: Path,
) -> None:
    flags = {
        "DFE_INFRA_UIS_EXTERNAL": "false",
        "DFE_UI_EXTERNAL": "false",
        "DFE_ENGINE_API_EXTERNAL": "false",
    }
    work = tmp_path / "checkout"
    for entry in (
        "Makefile",
        "scripts",
        "service_profiles.yaml",
        "compose",
        "config",
        "docker-compose.yml",
    ):
        source = REPO_ROOT / entry
        if source.is_dir():
            shutil.copytree(
                source, work / entry, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            work.mkdir(exist_ok=True)
            shutil.copy(source, work / entry)
    made = subprocess.run(
        ["make", "-s", "print-compose-file"],
        capture_output=True,
        check=False,
        cwd=work,
        encoding="utf-8",
        env={**os.environ, "DFE_PROFILE": "single-kafka", **flags},
        errors="replace",
        text=True,
    )
    assert made.returncode == 0, made.stderr
    target = _install(
        tmp_path / "install",
        settings="".join(f"{key}={value}\n" for key, value in flags.items()),
    )

    result, _ = _apply(tmp_path / "install", target, "--no-start")

    assert result.returncode == 0, result.stderr
    assert (
        _fragment(target, "docker-compose.unpublish.yml")
        == (work / ".tmp" / "docker-compose.unpublish.yml")
        .read_text(encoding="utf-8")
        .splitlines()[1:]
    )


def test_a_receiver_port_key_publishes_that_listener(tmp_path: Path) -> None:
    target = _install(tmp_path, settings="DFE_RECEIVER_HEC_PORT=8088\n")

    result, _ = _apply(tmp_path, target, "--no-start")

    assert result.returncode == 0, result.stderr
    assert _fragment(target, "docker-compose.receiver-ports.yml") == [
        "services:",
        "  dfe-receiver:",
        "    ports:",
        "      - ${DFE_INGRESS_BIND_HOST:-0.0.0.0}:${DFE_RECEIVER_HEC_PORT}:8088",
    ]


@pytest.mark.parametrize(
    ("settings", "complaint"),
    [
        ("DFE_UI_EXTERNAL=yes\n", "DFE_UI_EXTERNAL must be true or false"),
        ("DFE_BIND_SCOPE=lan\n", "DFE_BIND_SCOPE must be localhost or all"),
        (
            "DFE_BIND_SCOPE=all\n",
            "DFE_EXTERNAL_ORIGIN must name the address browsers use",
        ),
        ("DFE_DATA_ROOT=data\n", "DFE_DATA_ROOT must be an absolute path"),
        ("DFE_PROXY_TLS=true\n", "must start with https://"),
        (
            "DFE_PROXY_TLS=true\nDFE_EXTERNAL_ORIGIN=https://dfe.example.com\nDFE_NETWORK_SUBNET=8.8.8.0/24\n",
            "outside private address space",
        ),
        (
            "DFE_PROXY_TLS=true\nDFE_EXTERNAL_ORIGIN=https://dfe.example.com\nDFE_NETWORK_SUBNET=10.207.0.5/24\n",
            "host bits set",
        ),
        (
            "DFE_PROXY_TLS=true\nDFE_EXTERNAL_ORIGIN=https://dfe.example.com\nDFE_NETWORK_SUBNET=10.207.0.0/28\n",
            "outside /16 to /24",
        ),
        (
            "DFE_PROXY_TLS=true\nDFE_EXTERNAL_ORIGIN=https://dfe.example.com\nDFE_PROXY_CERT_DIR=certs\n",
            "must start with /, ./ or ../",
        ),
    ],
)
def test_a_setting_apply_sh_cannot_honour_stops_it_before_docker_starts(
    tmp_path: Path, settings: str, complaint: str
) -> None:
    target = _install(tmp_path, settings=settings)

    result, calls = _apply(tmp_path, target)

    assert result.returncode == 1
    assert complaint in result.stderr
    assert "up" not in calls.split()
    assert "## BEGIN apply.sh" not in (target / ".env").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "subnet", ["10.207.0.0/24", "172.20.0.0/16", "192.168.5.0/24", "10.8.0.0/18"]
)
@needs_openssl
def test_console_tls_mints_a_certificate_and_derives_the_address_the_makefile_does(
    tmp_path: Path, subnet: str
) -> None:
    certs = tmp_path / "certs"
    target = _install(
        tmp_path,
        settings=(
            "DFE_PROXY_TLS=true\nDFE_EXTERNAL_ORIGIN=https://dfe.example.com\n"
            f"DFE_NETWORK_SUBNET={subnet}\nDFE_PROXY_CERT_DIR={certs}\n"
        ),
    )

    result, _ = _apply(tmp_path, target, "--no-start")

    assert result.returncode == 0, result.stderr
    proxy_ip, ip_range = proxy_tls.derive(subnet=subnet)
    values = _values(target)
    assert values["DFE_PROXY_IP"] == proxy_ip
    assert values["DFE_NETWORK_IP_RANGE"] == ip_range
    assert values["COMPOSE_FILE"].endswith(":compose/tls.yml")
    assert (certs / "console.key").stat().st_mode & 0o777 == stat.S_IRUSR | stat.S_IWUSR
    assert not list(certs.glob(".mint.*"))
    verified = subprocess.run(
        [
            "openssl",
            "verify",
            "-CAfile",
            str(certs / "ca.crt"),
            str(certs / "console.crt"),
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    names = subprocess.run(
        [
            "openssl",
            "x509",
            "-in",
            str(certs / "console.crt"),
            "-noout",
            "-ext",
            "subjectAltName",
        ],
        capture_output=True,
        check=True,
        encoding="utf-8",
        text=True,
    ).stdout
    assert "DNS:dfe.example.com" in names
    assert "DNS:localhost" in names
    assert "IP Address:127.0.0.1" in names


@needs_openssl
def test_console_tls_keeps_a_certificate_set_that_is_already_there(
    tmp_path: Path,
) -> None:
    certs = tmp_path / "certs"
    target = _install(
        tmp_path,
        settings=f"DFE_PROXY_TLS=true\nDFE_EXTERNAL_ORIGIN=https://dfe.example.com\nDFE_PROXY_CERT_DIR={certs}\n",
    )
    _apply(tmp_path, target, "--no-start")
    before = (certs / "console.crt").read_bytes()

    result, _ = _apply(tmp_path, target, "--no-start")

    assert result.returncode == 0, result.stderr
    assert (certs / "console.crt").read_bytes() == before
    (certs / "ca.crt").unlink()
    refused, _ = _apply(tmp_path, target, "--no-start")
    assert refused.returncode == 1
    assert "holds only some of the certificate set" in refused.stderr


@pytest.mark.parametrize(
    ("tls", "network", "refused"),
    [
        ("true", "172.19.0.0/16", True),
        ("true", "10.207.0.0/24", False),
        ("false", "10.207.0.0/24", True),
        ("false", "172.19.0.0/16", False),
    ],
)
def test_flipping_console_tls_on_a_live_network_is_refused_until_it_is_down(
    tmp_path: Path, tls: str, network: str, refused: bool
) -> None:
    certs = tmp_path / "certs"
    for name in ("console.crt", "console.key", "ca.crt"):
        certs.mkdir(exist_ok=True)
        (certs / name).write_text("x", encoding="utf-8")
    target = _install(
        tmp_path,
        settings=f"DFE_PROXY_TLS={tls}\nDFE_EXTERNAL_ORIGIN=https://dfe.example.com\nDFE_PROXY_CERT_DIR={certs}\n",
    )

    result, calls = _apply(tmp_path, target, net_subnets=network)

    assert (result.returncode == 1) is refused
    assert ("Docker cannot re-address a live network" in result.stderr) is refused
    assert ("compose up" in calls) is not refused
