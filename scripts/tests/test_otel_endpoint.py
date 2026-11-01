#  Project:      dfe-docker
#  File:         tests/test_otel_endpoint.py
#  Purpose:      Assert each service is handed the OTLP transport its exporter speaks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The OTLP endpoint each service pushes to, and the transport it implies.

The scalo services export OTLP over gRPC and take the collector's 4317. dfe-ui's
exporter has no gRPC transport: handed 4317 it sends HTTP to a gRPC listener and
every span is dropped. So dfe-ui takes the same collector's HTTP port, 4318.
"""

import re

import pytest

import resolve_profile
from _common import COMPOSE_FILE

# A service key in docker-compose.yml: two spaces in, alone on its line.
_SERVICE_KEY = re.compile(r"^  ([a-z0-9-]+):\s*$")
_GRPC_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT: ${DFE_OTEL_EXPORTER_ENDPOINT:-}"
_HTTP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT: ${DFE_OTEL_EXPORTER_HTTP_ENDPOINT:-}"


def _service_lines() -> dict[str, list[str]]:
    services: dict[str, list[str]] = {}
    current = None
    in_services = False
    for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if in_services and line and not line.startswith((" ", "#")):
            in_services = False
            current = None
        if not in_services:
            continue
        key = _SERVICE_KEY.match(line)
        if key:
            current = key.group(1)
            services[current] = []
        elif current:
            services[current].append(line.strip())
    return services


@pytest.mark.parametrize(
    ("grpc", "http"),
    [
        ("http://otel-collector:4317", "http://otel-collector:4318"),
        ("https://collector.example.test:4317", "https://collector.example.test:4318"),
        ("https://collector.example.test:4318", "https://collector.example.test:4318"),
        ("https://otlp.example.test", "https://otlp.example.test"),
    ],
)
def test_the_http_endpoint_is_the_grpc_one_on_4318(grpc: str, http: str) -> None:
    assert resolve_profile.otel_http_endpoint(grpc) == http


def test_the_ui_pushes_otlp_over_http() -> None:
    ui = _service_lines()["dfe-ui"]
    assert _HTTP_ENDPOINT in ui
    assert "OTEL_EXPORTER_OTLP_PROTOCOL: http/protobuf" in ui


def test_no_other_service_is_moved_off_grpc() -> None:
    services = _service_lines()
    pushing = [name for name, lines in services.items() if _GRPC_ENDPOINT in lines]
    assert pushing, "no service pushes OTLP over gRPC -- the parse found nothing"
    for name, lines in services.items():
        if name == "dfe-ui":
            continue
        assert _HTTP_ENDPOINT not in lines, name
        assert not any(
            line.startswith("OTEL_EXPORTER_OTLP_PROTOCOL") for line in lines
        ), name


def _resolved(*, tmp_path, monkeypatch, environ: dict[str, str]) -> dict[str, str]:
    """Run the resolver and read back the assignments it wrote."""
    written = tmp_path / ".tmp" / "profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    for key in (
        resolve_profile.OTEL_ENDPOINT_ENV_VAR,
        resolve_profile.OTEL_HTTP_ENDPOINT_ENV_VAR,
        resolve_profile.OTEL_ENGINE_BACKEND_ENV_VAR,
        resolve_profile.OTEL_ENABLED_ENV_VAR,
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    assert resolve_profile.main() == 0
    values: dict[str, str] = {}
    for line in written.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


@pytest.mark.parametrize(
    ("environ", "endpoint", "http", "backend"),
    [
        pytest.param(
            {"DFE_OTEL_ENABLED": "true"},
            "http://otel-collector:4317",
            "http://otel-collector:4318",
            "opentelemetry",
            id="bundled",
        ),
        pytest.param(
            {
                "DFE_OTEL_ENABLED": "false",
                "DFE_OTEL_EXPORTER_ENDPOINT": "https://collector.example.test:4317",
            },
            "https://collector.example.test:4317",
            "https://collector.example.test:4318",
            "opentelemetry",
            id="external-only",
        ),
        pytest.param({"DFE_OTEL_ENABLED": "false"}, "", "", "", id="none"),
    ],
)
def test_every_service_follows_whichever_otlp_endpoint_is_configured(
    tmp_path,
    monkeypatch,
    dotenv,
    environ: dict[str, str],
    endpoint: str,
    http: str,
    backend: str,
) -> None:
    """An external collector with the bundled one off still gets the engine and the UI."""
    values = _resolved(tmp_path=tmp_path, monkeypatch=monkeypatch, environ=environ)

    assert values[resolve_profile.OTEL_ENDPOINT_ENV_VAR] == endpoint
    assert values[resolve_profile.OTEL_HTTP_ENDPOINT_ENV_VAR] == http
    assert values[resolve_profile.OTEL_ENGINE_BACKEND_ENV_VAR] == backend
