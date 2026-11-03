#  Project:      dfe-docker
#  File:         tests/test_bundle.py
#  Purpose:      Assert the release bundle holds what install.sh reads and none of what it must not carry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on scripts/bundle.py that need no registry: the profile and pin files it derives and the file set it packs."""

import tarfile
from pathlib import Path

import pytest

import bundle
import init

MK = """export PROFILE_FLAGS := --profile clickhouse --profile kafka-redpanda
export DFE_SERVICES := dfe-loader dfe-receiver kafka-ui
export DFE_KAFKA_BOOTSTRAP_SERVERS := kafka:9092
export DFE_RECEIVER_DLQ_MODE :=
export DFE_OTEL_RESOLVED := true
export DFE_ADMIN_LINKS_RESOLVED := [{"name":"Kafbat","url":"http://localhost:8081"}]
export DFE_USAGE_CPU_LIMITS := {"dfe-loader":2.0,"kafka-ui":-1.0}
export DFE_USAGE_NODE_NAME := docker-host
not an export line
"""


def test_a_resolved_profile_becomes_dotenv_and_a_shell_conf() -> None:
    dotenv, conf, limits = bundle.profile_files(mk_text=MK)

    assert dotenv.splitlines() == [
        "DFE_KAFKA_BOOTSTRAP_SERVERS=kafka:9092",
        "DFE_RECEIVER_DLQ_MODE=",
        "DFE_OTEL_RESOLVED=true",
        'DFE_ADMIN_LINKS_RESOLVED=\'[{"name":"Kafbat","url":"http://localhost:8081"}]\'',
    ]
    assert conf.splitlines() == [
        'PROFILES="clickhouse,kafka-redpanda"',
        'SERVICES="dfe-loader dfe-receiver kafka-ui"',
    ]
    assert limits == '{"dfe-loader":2.0,"kafka-ui":-1.0}'


def test_a_value_with_a_single_quote_is_refused_rather_than_misquoted() -> None:
    with pytest.raises(bundle.BundleError, match="single quote"):
        bundle.dotenv_value(value="it's")


def test_only_digested_pins_and_the_stack_marker_are_kept() -> None:
    text = (
        "# a comment\n"
        "DFE_ENGINE_VERSION=v1.2.3@sha256:" + "a" * 64 + "  # ghcr.io/x\n"
        "DFE_STACK_VERSION=2.2.1  # the stack these came from\n"
        "DFE_UI_PORT=3000\n"
        "# DFE_LOADER_VERSION=\n"
        "CLICKHOUSE_PASSWORD=secret\n"
    )

    assert bundle.pin_lines(dotenv_text=text).splitlines() == [
        "DFE_ENGINE_VERSION=v1.2.3@sha256:" + "a" * 64,
        "DFE_STACK_VERSION=2.2.1",
    ]


def test_a_stack_with_no_digested_pin_is_refused() -> None:
    with pytest.raises(bundle.BundleError, match="no image pin"):
        bundle.pin_lines(dotenv_text="DFE_STACK_VERSION=2.2.1\n")


def test_every_generated_secret_is_listed_with_its_kind_and_length() -> None:
    rows = [line.split() for line in bundle.secrets_list().splitlines()]

    minted = {(file, key): int(length) for file, key, _, length in rows}
    expected = {
        (file, key): length
        for file, keys in init.GENERATED_SECRETS.items()
        for key, length in keys.items()
    }
    assert minted == expected
    assert {key for _, key, kind, _ in rows if kind == "hex"} == init._HEX_SECRETS


def test_the_bundle_carries_no_secret_and_no_dev_tooling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bundle, "resolve", lambda *, profile, **_: MK)
    pins = "DFE_ENGINE_VERSION=v1@sha256:" + "b" * 64 + "\n"
    out, _ = bundle.build(out=tmp_path, pins=pins, version="9.9.9")

    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()

    assert "dfe/docker-compose.yml" in names
    assert "dfe/install/stack.env" in names
    assert "dfe/install/profiles/single-kafka.conf" in names
    assert "dfe/install/profiles/single-kafka.usage" in names
    assert {
        f"dfe/install/profiles/single-kafka.{variant}.env"
        for variant in ("kafbat", "hyperdx", "kafbat-hyperdx")
    } <= set(names)
    assert "dfe/apply.sh" in names
    with tarfile.open(out, "r:gz") as tar:
        assert tar.getmember("dfe/apply.sh").mode == 0o755
        assert tar.getmember("dfe/docker-compose.yml").mode == 0o644
    assert not [n for n in names if n in ("dfe/.env", "dfe/deployment.yaml")]
    assert not [
        n
        for n in names
        if n.startswith(
            ("dfe/env/", "dfe/scripts/", "dfe/tests/", "dfe/ops/", "dfe/docker/")
        )
    ]
    assert (tmp_path / "SHA256SUMS").read_text(encoding="utf-8").split()[1] == out.name


def test_a_profile_with_a_collector_and_no_cpu_limits_stops_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        bundle,
        "resolve",
        lambda *, profile, **_: MK.replace(
            'export DFE_USAGE_CPU_LIMITS := {"dfe-loader":2.0,"kafka-ui":-1.0}',
            "export DFE_USAGE_CPU_LIMITS := {}",
        ),
    )

    with pytest.raises(bundle.BundleError, match="no CPU limits"):
        bundle.build(out=tmp_path, pins="DFE_ENGINE_VERSION=v1@sha256:x\n", version="9")
