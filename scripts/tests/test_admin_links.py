#  Project:      dfe-docker
#  File:         tests/test_admin_links.py
#  Purpose:      Assert the engine is handed exactly the admin UIs the stack runs
#                and publishes, at the URL a browser opens
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The admin links resolve_profile hands dfe-engine, and the path they take to it.

A UI is listed only when the profile runs it and the exposure dials publish it. A
listed UI the dials took off the host is a dead link, and a running one left off
the list is a console nobody finds.

The matrix drives resolve_profile.main() with the `--unpublished` arguments the
Makefile passes. The make tests run the real Makefile in a copy of the checkout,
from the exposure flags an operator sets, and read the engine's environment out
of `docker compose config` -- so the JSON is shown to survive make, the
environment and compose interpolation unchanged. Nothing is started.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import instances
import resolve_profile
from _common import COMPOSE_LOCAL_FILE, _required_compose_vars

REPO_ROOT = Path(__file__).resolve().parents[2]

_ORIGIN = "http://dfe.example.test"
_PROXY_ORIGIN = "https://edge.example.test"
_HYPERDX_APP_URL = "http://hyperdx.example.test"

_KAFBAT = {
    "name": "Kafbat",
    "purpose": "Topics, consumer groups and lag",
    "probe_url": "http://kafka-ui:8080",
}
_HYPERDX = {
    "name": "HyperDX",
    "purpose": "Logs, metrics and traces search",
    "probe_url": "http://dfe-hyperdx-proxy:8090",
}

# A profile per transport that declares no footprint key, so the env vars decide.
_TRANSPORT_PROFILES = {"kafka": "kafka-minimal", "grpc": "grpc-minimal"}

# Placeholders for the settings an armed auth profile refuses to resolve without.
_AUTH_SETTINGS = {
    "DFE_AUTH_ENABLED": "true",
    "DFE_OIDC_ISSUER_URL": "https://idp.example.invalid/realms/dfe",
    "DFE_OIDC_CLIENT_ID": "placeholder",
    "DFE_OIDC_CLIENT_SECRET": "placeholder",
    "DFE_OAUTH2_PROXY_COOKIE_SECRET": "placeholder",
}

# Every key that moves the list, so the developer's own environment decides nothing.
_RESOLUTION_KEYS = (
    "DFE_PROFILE",
    "KAFKA_BACKEND",
    "KAFBAT_ENABLED",
    "DFE_HYPERDX_ENABLED",
    "DFE_AUTH_ENABLED",
    "DFE_CORE_ENABLED",
    "DFE_CLICKHOUSE_ENABLED",
    "DFE_OTEL_ENABLED",
    "DFE_EXTERNAL_ORIGIN",
    "DFE_OAUTH2_PROXY_EXTERNAL_ORIGIN",
    "DFE_HYPERDX_APP_URL",
    "KAFBAT_PORT",
    "DFE_HYPERDX_APP_PORT",
    "DFE_OIDC_ALLOWED_GROUPS",
    *_AUTH_SETTINGS,
)

# The fields dfe_engine.admin_links.AdminLink accepts, and it refuses any other.
_ENGINE_FIELDS = {"name", "purpose", "url", "probe_url"}


def _kafbat(url: str) -> dict[str, str]:
    return {**_KAFBAT, "url": url}


def _hyperdx(url: str) -> dict[str, str]:
    return {**_HYPERDX, "url": url}


def _exports(*, path: Path) -> dict[str, str]:
    """Return the make exports one .profile.mk carries."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


def _engine_refusals(entry: object) -> list[str]:
    """Return why dfe-engine's AdminLink would drop this entry, empty when it takes it."""
    if not isinstance(entry, dict):
        return ["not an object"]
    refusals = [f"unknown field {key!r}" for key in sorted(set(entry) - _ENGINE_FIELDS)]
    for field in ("name", "purpose", "url"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            refusals.append(f"{field} is missing or empty")
    for field in ("url", "probe_url"):
        value = entry.get(field)
        if not isinstance(value, str):
            continue
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            refusals.append(f"{field} is not an absolute http(s) URL")
        if parts.username is not None or parts.password is not None:
            refusals.append(f"{field} carries credentials")
        try:
            port = parts.port
        except ValueError:
            refusals.append(f"{field} names a port out of range")
            continue
        if port == 0:
            refusals.append(f"{field} names port 0")
    return refusals


def _links(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: str,
    unpublished: tuple[str, ...] = (),
    **environment: str,
) -> list[dict[str, str]]:
    """Return the admin links resolve_profile writes for one profile."""
    written = tmp_path / ".profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    # A tier the engine renders nothing for removes the instances fragment.
    monkeypatch.setattr(
        instances, "COMPOSE_INSTANCES_FILE", tmp_path / "docker-compose.instances.yml"
    )
    # Set before deleting, so teardown also removes what .env loads during the run.
    for key in _RESOLUTION_KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv("DFE_PROFILE", profile)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    argv = [arg for ui in unpublished for arg in ("--unpublished", ui)]
    assert resolve_profile.main(argv) == 0

    return json.loads(_exports(path=written)[resolve_profile.ADMIN_LINKS_VAR])


@pytest.mark.parametrize("origin", ["", _ORIGIN], ids=["scope-localhost", "scope-all"])
@pytest.mark.parametrize("auth", [False, True], ids=["open", "auth"])
@pytest.mark.parametrize(
    "unpublished",
    [(), ("kafbat",), ("hyperdx",), ("hyperdx", "kafbat")],
    ids=["all-published", "kafbat-gated", "hyperdx-gated", "infra-killed"],
)
@pytest.mark.parametrize("hyperdx", [False, True], ids=["no-hyperdx", "hyperdx"])
@pytest.mark.parametrize("kafbat", [False, True], ids=["no-kafbat", "kafbat"])
@pytest.mark.parametrize("transport", sorted(_TRANSPORT_PROFILES))
def test_a_ui_is_listed_exactly_where_it_runs_and_is_published(
    auth: bool,
    dotenv: Path,
    hyperdx: bool,
    kafbat: bool,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    tmp_path: Path,
    transport: str,
    unpublished: tuple[str, ...],
) -> None:
    environment = {
        "KAFBAT_ENABLED": str(kafbat).lower(),
        "DFE_HYPERDX_ENABLED": str(hyperdx).lower(),
    }
    if auth:
        environment.update(_AUTH_SETTINGS)
    if origin:
        environment["DFE_EXTERNAL_ORIGIN"] = origin

    links = _links(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        profile=_TRANSPORT_PROFILES[transport],
        unpublished=unpublished,
        **environment,
    )

    browser = origin or "http://localhost"
    want = []
    # No broker, no Kafka UI: the grpc transport never starts Kafbat.
    if transport == "kafka" and kafbat and "kafbat" not in unpublished:
        want.append(_kafbat(f"{browser}:8081"))
    if hyperdx and "hyperdx" not in unpublished:
        want.append(_hyperdx(f"{browser}:8090"))
    assert links == want
    assert [_engine_refusals(entry) for entry in links] == [[] for _ in links]


@pytest.mark.parametrize(
    ("profile", "want"),
    [
        ("slim", [_hyperdx("http://localhost:8090")]),
        (
            "single",
            [_kafbat("http://localhost:8081"), _hyperdx("http://localhost:8090")],
        ),
    ],
)
def test_the_projected_tiers_list_what_their_footprint_runs(
    dotenv: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    tmp_path: Path,
    want: list[dict[str, str]],
) -> None:
    assert _links(monkeypatch=monkeypatch, tmp_path=tmp_path, profile=profile) == want


def test_behind_the_auth_profile_both_links_take_the_proxy_origin(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The browser comes back on the proxy's callback, so that is where it is sent."""
    links = _links(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        profile="kafka-minimal",
        DFE_HYPERDX_ENABLED="true",
        DFE_EXTERNAL_ORIGIN=_ORIGIN,
        DFE_OAUTH2_PROXY_EXTERNAL_ORIGIN=_PROXY_ORIGIN,
        DFE_HYPERDX_APP_URL=_HYPERDX_APP_URL,
        **_AUTH_SETTINGS,
    )

    assert links == [
        _kafbat(f"{_PROXY_ORIGIN}:8081"),
        _hyperdx(f"{_PROXY_ORIGIN}:8090"),
    ]


def test_without_auth_hyperdx_takes_its_own_app_url(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no proxy running, the proxy origin names nothing and the app URL wins."""
    links = _links(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        profile="kafka-minimal",
        DFE_HYPERDX_ENABLED="true",
        DFE_EXTERNAL_ORIGIN=_ORIGIN,
        DFE_OAUTH2_PROXY_EXTERNAL_ORIGIN=_PROXY_ORIGIN,
        DFE_HYPERDX_APP_URL=_HYPERDX_APP_URL,
    )

    assert links == [
        _kafbat(f"{_ORIGIN}:8081"),
        _hyperdx(f"{_HYPERDX_APP_URL}:8090"),
    ]


def test_a_moved_host_port_moves_the_link_and_not_the_probe(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    links = _links(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        profile="single",
        KAFBAT_PORT="18081",
        DFE_HYPERDX_APP_PORT="18090",
    )

    assert links == [
        _kafbat("http://localhost:18081"),
        _hyperdx("http://localhost:18090"),
    ]


def test_a_trailing_slash_on_the_origin_is_not_carried_into_the_url(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    links = _links(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        profile="slim",
        DFE_EXTERNAL_ORIGIN=f"{_ORIGIN}/",
    )

    assert links == [_hyperdx(f"{_ORIGIN}:8090")]


def test_the_origin_is_read_from_dotenv_where_an_operator_sets_it(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Compose reads .env for the port bindings, so the links read the same file."""
    dotenv.write_text(
        f"DFE_EXTERNAL_ORIGIN={_ORIGIN}\nDFE_HYPERDX_APP_PORT=18090\n",
        encoding="utf-8",
        newline="\n",
    )

    links = _links(monkeypatch=monkeypatch, tmp_path=tmp_path, profile="slim")

    assert links == [_hyperdx(f"{_ORIGIN}:18090")]


def test_an_unpublished_name_the_resolver_does_not_know_is_refused(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Makefile typo must stop the run, not leave a gated UI on the list."""
    with pytest.raises(SystemExit) as stopped:
        _links(
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
            profile="single",
            unpublished=("kafka-ui",),
        )

    assert stopped.value.code == 2


@pytest.mark.parametrize(
    ("entry", "refusal"),
    [
        ({**_hyperdx("http://localhost:8090"), "icon": "x"}, "unknown field 'icon'"),
        (_hyperdx("http://someone@localhost:8090"), "url carries credentials"),
        (_hyperdx("localhost:8090"), "url is not an absolute http(s) URL"),
        ({**_hyperdx("http://localhost:8090"), "purpose": " "}, "purpose is missing"),
    ],
)
def test_the_shape_check_refuses_what_the_engine_refuses(
    entry: dict[str, str], refusal: str
) -> None:
    """Without this the matrix's shape assertion could pass on anything."""
    assert any(message.startswith(refusal) for message in _engine_refusals(entry))


# ---------------------------------------------------------------------------
# make -> environment -> compose
# ---------------------------------------------------------------------------

_PROBE_GOAL = "admin-links-probe"
# The file set and profile flags `make ci` hands compose, rendering the engine
# instead of starting it.
_PROBE_MAKEFILE = f"""\
include Makefile

{_PROBE_GOAL}:
\t@docker compose -f docker-compose.yml $(STORAGE_FLAGS) $(UI_FLAGS) $(PROFILE_FLAGS) config --format json dfe-engine
"""
# Enough to satisfy the compose file's `${VAR:?...}` image pins.
_PLACEHOLDER = "0.0.0-admin-links-test"
# Keys whose value in the developer's shell would otherwise reach make or compose.
_FOREIGN_PREFIXES = ("DFE_", "KAFBAT_", "KAFKA_", "HYPERDX_", "CLICKHOUSE_", "COMPOSE_")
_MAKE_STATE = ("MAKEFLAGS", "MAKELEVEL", "MFLAGS")
# Written by a make run, so never copied from the checkout into the scratch one.
_GENERATED = {instances.COMPOSE_INSTANCES_FILE.name, COMPOSE_LOCAL_FILE.name}


def _compose_renders() -> bool:
    """Whether this machine has the make and docker compose the path needs."""
    if shutil.which("make") is None or shutil.which("docker") is None:
        return False
    version = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False
    )
    return version.returncode == 0


_needs_compose = pytest.mark.skipif(
    not _compose_renders(),
    reason="make or docker compose is not installed, so the path cannot be rendered",
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A copy of what make and compose read, with no .env and no resolved profile."""
    shutil.copy(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copy(REPO_ROOT / "service_profiles.yaml", tmp_path / "service_profiles.yaml")
    for compose in REPO_ROOT.glob("docker-compose*.yml"):
        if compose.name not in _GENERATED:
            shutil.copy(compose, tmp_path / compose.name)
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    shutil.copytree(
        REPO_ROOT / "scripts",
        tmp_path / "scripts",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    (tmp_path / "probe.mk").write_text(_PROBE_MAKEFILE, encoding="utf-8", newline="\n")
    return tmp_path


def _through_make(*, cwd: Path, **settings: str) -> tuple[str, str]:
    """Return (the .profile.mk value, the engine's DFE_ADMIN_LINKS) for one make run."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_FOREIGN_PREFIXES) and key not in _MAKE_STATE
    }
    pins = _required_compose_vars(files=(cwd / "docker-compose.yml",))
    environment.update({name: _PLACEHOLDER for name in pins})
    environment.update(settings)
    result = subprocess.run(
        ["make", "--no-print-directory", "-f", "probe.mk", _PROBE_GOAL],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    engine = json.loads(result.stdout)["services"]["dfe-engine"]["environment"]
    resolved = _exports(path=cwd / ".profile.mk")[resolve_profile.ADMIN_LINKS_VAR]
    return resolved, engine["DFE_ADMIN_LINKS"]


_SINGLE_ALL = {
    "DFE_PROFILE": "single",
    "DFE_BIND_SCOPE": "all",
    "DFE_EXTERNAL_ORIGIN": _ORIGIN,
}
_BOTH_ON_ORIGIN = [_kafbat(f"{_ORIGIN}:8081"), _hyperdx(f"{_ORIGIN}:8090")]


@_needs_compose
@pytest.mark.parametrize(
    ("settings", "want"),
    [
        ({}, [_hyperdx("http://localhost:8090")]),
        (_SINGLE_ALL, _BOTH_ON_ORIGIN),
        ({**_SINGLE_ALL, "DFE_INFRA_UIS_EXTERNAL": "false"}, []),
        (
            {**_SINGLE_ALL, "DFE_KAFBAT_UI_EXTERNAL": "false"},
            [_hyperdx(f"{_ORIGIN}:8090")],
        ),
        (
            {**_SINGLE_ALL, "DFE_HYPERDX_UI_EXTERNAL": "false"},
            [_kafbat(f"{_ORIGIN}:8081")],
        ),
        (
            {
                **_SINGLE_ALL,
                **_AUTH_SETTINGS,
                "DFE_OAUTH2_PROXY_EXTERNAL_ORIGIN": _PROXY_ORIGIN,
            },
            [_kafbat(f"{_PROXY_ORIGIN}:8081"), _hyperdx(f"{_PROXY_ORIGIN}:8090")],
        ),
        ({**_SINGLE_ALL, **_AUTH_SETTINGS, "DFE_INFRA_UIS_EXTERNAL": "false"}, []),
    ],
    ids=[
        "slim-default",
        "single-scope-all",
        "single-infra-killed",
        "single-kafbat-unpublished",
        "single-hyperdx-unpublished",
        "single-auth",
        "single-auth-infra-killed",
    ],
)
def test_the_engine_receives_the_list_make_resolves_intact(
    checkout: Path, settings: dict[str, str], want: list[dict[str, str]]
) -> None:
    resolved, engine = _through_make(cwd=checkout, **settings)

    assert engine == resolved
    links = json.loads(engine)
    assert links == want
    assert [_engine_refusals(entry) for entry in links] == [[] for _ in links]
