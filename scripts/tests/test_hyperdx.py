#  Project:      dfe-docker
#  File:         tests/test_hyperdx.py
#  Purpose:      Assert the engine is pointed at HyperDX, and HyperDX at the engine
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The two halves of the HyperDX control surface, as a profile resolves them.

The engine drives HyperDX's team, connections and sources over a token it signs
itself, so it has to know HyperDX is there; HyperDX has to verify that token
against the engine's JWKS. With either half unset a source added through the
console never gets its HyperDX view, and the engine answers 503 `hyperdx_absent`.

Read the way compose reads it: the `.profile.mk` values resolve_profile writes for
a profile, expanded over the `${NAME:-default}` forms docker-compose.yml uses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import resolve_profile
from _common import COMPOSE_FILE, REPO_ROOT

_ENGINE_SERVICE = "dfe-engine"
_HYPERDX_SERVICE = "hyperdx"
_IN_STACK_HYPERDX = "http://hyperdx:8000"
_JWKS_URL = "http://dfe-engine:8000/.well-known/jwks.json"

_INTERPOLATION = re.compile(r"\$\{([A-Z0-9_]+):-([^{}]*)\}")

# Every key a profile resolution must decide for itself, whatever the developer's
# own environment says.
_FOOTPRINT_KEYS = (
    "DFE_PROFILE",
    "DFE_HYPERDX_ENABLED",
    "DFE_HYPERDX_BASE_URL",
    "DFE_HYPERDX_RESOLVED",
    "DFE_HYPERDX_RESOLVED_BASE_URL",
    "KAFKA_BACKEND",
)

# The identity a proxy used to stamp on every HyperDX request. `oidc-proxy`
# ignores it, so one left anywhere reads as an identity that still works.
_RETIRED_IDENTITY = ("DFE_HYPERDX_IDENTITY_", "X-OIDC-Subject", "X-OIDC-Groups")
_PROXY_CONFIGS = ("config/proxy/envoy.yaml", "config/proxy/hyperdx.yaml")


def _service_environment(*, service: str) -> dict[str, str]:
    """Return one service's `environment:` block as {key: compose template}."""
    found: dict[str, str] = {}
    in_service = False
    in_environment = False
    for raw in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not (line) or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 2:
            in_service = line == f"{service}:"
            in_environment = False
            continue
        if not (in_service):
            continue
        if indent == 4:
            in_environment = line == "environment:"
            continue
        if in_environment and indent == 6:
            key, _, value = line.partition(": ")
            found[key] = value
    if not (found):
        raise AssertionError(
            f"{COMPOSE_FILE.name} gives {service} no environment block"
        )
    return found


def _expand(*, template: str, values: dict[str, str]) -> str:
    """Resolve `${NAME:-default}` innermost first, as compose interpolates it."""
    while (match := _INTERPOLATION.search(template)) is not None:
        replacement = values.get(match.group(1), "").strip() or match.group(2)
        template = template[: match.start()] + replacement + template[match.end() :]
    return template


def _exports(
    *,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    tmp_path: Path,
    **environment: str,
) -> dict[str, str]:
    """Return the make exports resolve_profile writes for one profile."""
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", tmp_path / ".profile.mk")
    for key in _FOOTPRINT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DFE_PROFILE", profile)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    assert resolve_profile.main() == 0

    values: dict[str, str] = {}
    for line in (tmp_path / ".profile.mk").read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


def _engine_hyperdx(*, exports: dict[str, str]) -> tuple[str, str]:
    """Return the (enabled, base URL) the engine container receives."""
    environment = _service_environment(service=_ENGINE_SERVICE)
    return (
        _expand(template=environment["DFE_HYPERDX_ENABLED"], values=exports),
        _expand(template=environment["DFE_HYPERDX_BASE_URL"], values=exports),
    )


@pytest.mark.parametrize("profile", ["single", "slim"])
def test_a_tier_that_runs_hyperdx_points_the_engine_at_it(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, profile: str, tmp_path: Path
) -> None:
    exports = _exports(monkeypatch=monkeypatch, profile=profile, tmp_path=tmp_path)

    assert _engine_hyperdx(exports=exports) == ("true", _IN_STACK_HYPERDX)


def test_a_tier_without_hyperdx_leaves_the_engine_holding_nothing(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exports = _exports(
        monkeypatch=monkeypatch, profile="kafka-minimal", tmp_path=tmp_path
    )

    assert _engine_hyperdx(exports=exports) == ("false", "")


def test_the_operator_toggle_moves_the_engine_with_the_containers(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One DFE_HYPERDX_ENABLED, whether it comes from the profile or from .env."""
    exports = _exports(
        monkeypatch=monkeypatch,
        profile="kafka-minimal",
        tmp_path=tmp_path,
        DFE_HYPERDX_ENABLED="true",
    )

    assert _HYPERDX_SERVICE in exports["DFE_SERVICES"].split()
    assert _engine_hyperdx(exports=exports) == ("true", _IN_STACK_HYPERDX)


def test_an_external_hyperdx_is_the_one_the_engine_is_given(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    external = "http://observability.example.test:8000"
    exports = _exports(
        monkeypatch=monkeypatch,
        profile="single",
        tmp_path=tmp_path,
        DFE_HYPERDX_BASE_URL=external,
    )

    assert _engine_hyperdx(exports=exports) == ("true", external)


def test_hyperdx_verifies_the_engines_token() -> None:
    environment = _service_environment(service=_HYPERDX_SERVICE)

    assert _expand(template=environment["DFE_AUTH_MODE"], values={}) == "oidc-proxy"
    assert _expand(template=environment["DFE_ENGINE_JWKS_URL"], values={}) == _JWKS_URL


def test_a_developer_with_no_engine_can_still_select_header_dev() -> None:
    """The compose `environment:` beats env/hyperdx.env, so the switch lives here."""
    environment = _service_environment(service=_HYPERDX_SERVICE)

    assert (
        _expand(
            template=environment["DFE_AUTH_MODE"],
            values={"DFE_HYPERDX_AUTH_MODE": "header-dev"},
        )
        == "header-dev"
    )


def test_nothing_stamps_an_identity_hyperdx_no_longer_reads() -> None:
    texts = {
        name: (REPO_ROOT / name).read_text(encoding="utf-8") for name in _PROXY_CONFIGS
    }
    texts[COMPOSE_FILE.name] = COMPOSE_FILE.read_text(encoding="utf-8")

    for name, text in sorted(texts.items()):
        for retired in _RETIRED_IDENTITY:
            assert retired not in text, f"{name} still carries {retired}"
