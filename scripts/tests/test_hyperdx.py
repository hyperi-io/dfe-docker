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
Its session signing key comes from `make init`, or the fork signs with a key
published upstream; so does its token encryption key, in a format it parses.

Read the way compose reads it: the `.tmp/profile.mk` values resolve_profile writes for a profile, expanded over the `${NAME:-default}` forms docker-compose.yml uses.
"""

import re
from pathlib import Path

import pytest

import init
import post
import resolve_profile
from _common import COMPOSE_FILE, REPO_ROOT

_ENGINE_SERVICE = "dfe-engine"
_HYPERDX_SERVICE = "hyperdx"
_SESSION_KEY = "HYPERDX_EXPRESS_SESSION_SECRET"
_TOKEN_KEY = "HYPERDX_TOKEN_ENCRYPTION_KEY"
# The hex form of the two the fork's tokenEncryption.ts parseEncryptionKey accepts.
_HEX_AES256 = re.compile(r"[0-9a-f]{64}")
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

# The dials for the fixed identity a proxy used to stamp on every HyperDX request.
# `oidc-proxy` ignores it, so one left anywhere reads as an identity that still
# works. check_compose holds the proxies to adding no identity header at all.
_RETIRED_IDENTITY = "DFE_HYPERDX_IDENTITY_"
_PROXY_CONFIGS = (
    "config/proxy/envoy.yaml",
    "config/proxy/envoy.tls.yaml",
    "config/proxy/hyperdx.yaml",
    "config/proxy/hyperdx.tls.yaml",
)


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
    written = tmp_path / ".tmp" / "profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    # Set before deleting, so teardown also removes what .env loads during the run.
    for key in _FOOTPRINT_KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv("DFE_PROFILE", profile)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    assert resolve_profile.main() == 0

    values: dict[str, str] = {}
    for line in written.read_text(encoding="utf-8").splitlines():
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


def test_hyperdx_does_not_start_without_core(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exports = _exports(
        monkeypatch=monkeypatch,
        profile="single",
        tmp_path=tmp_path,
        DFE_CORE_ENABLED="false",
        DFE_HYPERDX_ENABLED="true",
    )

    assert _HYPERDX_SERVICE not in exports["DFE_SERVICES"].split()
    assert _engine_hyperdx(exports=exports) == ("false", "")


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

    # A literal, so no .env or env/hyperdx.env value can switch the mode
    assert environment["DFE_AUTH_MODE"] == "oidc-proxy"
    assert _expand(template=environment["DFE_ENGINE_JWKS_URL"], values={}) == _JWKS_URL


def test_hyperdx_signs_sessions_with_the_key_make_init_mints() -> None:
    """Unset, the fork signs sessions with a key published in upstream's source."""
    template = _service_environment(service=_HYPERDX_SERVICE)["EXPRESS_SESSION_SECRET"]
    minted = init._mint(_SESSION_KEY)

    assert init.GENERATED_SECRETS[_SESSION_KEY] >= 32
    assert _expand(template=template, values={_SESSION_KEY: minted}) == minted
    # The fallback compose runs on is exactly what `make post` refuses on HyperDX.
    assert post.WEAK_SECRET_DEFAULTS[_SESSION_KEY] == (
        _expand(template=template, values={}),
        _HYPERDX_SERVICE,
    )


def test_the_minted_token_key_is_one_hyperdx_parses() -> None:
    """HyperDX refuses to start unless the key is 64 hex chars or base64 of 32 bytes."""
    minted = init._mint(_TOKEN_KEY)

    assert _HEX_AES256.fullmatch(minted)
    assert len(bytes.fromhex(minted)) == 32


def test_hyperdx_encrypts_tokens_with_the_key_make_init_mints() -> None:
    template = _service_environment(service=_HYPERDX_SERVICE)["TOKEN_ENCRYPTION_KEY"]
    minted = init._mint(_TOKEN_KEY)

    assert _expand(template=template, values={_TOKEN_KEY: minted}) == minted
    # Empty reads as encryption off; a sentinel would read as a malformed key.
    assert _expand(template=template, values={}) == ""
    assert post.WEAK_SECRET_DEFAULTS[_TOKEN_KEY] == ("", _HYPERDX_SERVICE)


@pytest.mark.parametrize("key", [_SESSION_KEY, _TOKEN_KEY])
def test_an_env_that_predates_a_hyperdx_key_gets_one_and_keeps_it(
    dotenv: Path, key: str
) -> None:
    older = "".join(
        f"{other}=already-minted-value\n"
        for other in init.GENERATED_SECRETS
        if other != key
    )
    dotenv.write_text(older, encoding="utf-8", newline="\n")

    init._top_up_secrets(dotenv_path=dotenv)
    topped_up = dotenv.read_text(encoding="utf-8")
    init._top_up_secrets(dotenv_path=dotenv)

    assert topped_up.startswith(older)
    minted = [
        line.partition("=")[2]
        for line in topped_up.splitlines()
        if line.startswith(f"{key}=")
    ]
    assert [len(value) for value in minted] == [init.GENERATED_SECRETS[key]]
    assert dotenv.read_text(encoding="utf-8") == topped_up


def test_nothing_stamps_an_identity_hyperdx_no_longer_reads() -> None:
    texts = {
        name: (REPO_ROOT / name).read_text(encoding="utf-8") for name in _PROXY_CONFIGS
    }
    texts[COMPOSE_FILE.name] = COMPOSE_FILE.read_text(encoding="utf-8")

    for name, text in sorted(texts.items()):
        assert _RETIRED_IDENTITY not in text, (
            f"{name} still carries {_RETIRED_IDENTITY}"
        )
