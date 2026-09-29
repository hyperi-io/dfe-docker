#  Project:      dfe-docker
#  File:         tests/test_metrics_manifest.py
#  Purpose:      Assert the engine is told where each app serves its metric
#                manifest, at an address the Compose network resolves
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Where dfe-engine reads an app's live metric manifest on Compose.

A metrics refresh fetches DFE_SERVICES_METRICS_MANIFEST_URL with `{service}` filled
by the app's name. scalo serves `/metrics/manifest` on the listener that answers
`/livez`, so the address works only where the name is a service key on the engine's
network and the port is the container's, not the host port that publishes it. An
address with nothing behind it fails quietly: the refresh answers `refreshed: false`.

Read off the interpolated model `docker compose config` produces, for the reason
check_compose gives.
"""

import shutil
from urllib.parse import urlsplit

import pytest

import check_compose

_ENGINE_SERVICE = "dfe-engine"
_MANIFEST_ENV = "DFE_SERVICES_METRICS_MANIFEST_URL"
_MANIFEST_PATH = "/metrics/manifest"
# The apps dfe-engine ships a surface for (services/surfaces/resources/<app>.yaml).
_SURFACE_APPS = ("dfe-archiver", "dfe-loader", "dfe-receiver")


@pytest.fixture(scope="module")
def services() -> dict[str, dict]:
    """The rendered registry-path stack, placeholders for the pins."""
    if shutil.which("docker") is None:
        pytest.skip("`docker compose config` renders the model these assert on")
    env, _ = check_compose._check_env()
    config = check_compose._config_json(
        env=env, files=[check_compose.COMPOSE_FILE.name]
    )
    assert config is not None, "the registry compose path did not resolve"
    return config["services"]


def _template(services: dict[str, dict]) -> str:
    return services[_ENGINE_SERVICE]["environment"][_MANIFEST_ENV]


def test_the_engine_names_each_app_by_the_one_placeholder_it_fills(
    services: dict[str, dict],
) -> None:
    """The engine refuses to start on any other placeholder."""
    template = _template(services)

    assert template.count("{") == 1
    assert "{service}" in template


@pytest.mark.parametrize("app", _SURFACE_APPS)
def test_the_address_is_the_apps_own_health_listener(
    app: str, services: dict[str, dict]
) -> None:
    """The container port its healthcheck probes, never the port the host publishes."""
    url = urlsplit(_template(services).format(service=app))
    probe = urlsplit(services[app]["healthcheck"]["test"][-1])

    assert url.hostname == app
    assert url.path == _MANIFEST_PATH
    assert url.port == probe.port


@pytest.mark.parametrize("app", _SURFACE_APPS)
def test_the_engine_shares_a_network_with_the_app(
    app: str, services: dict[str, dict]
) -> None:
    """A service name resolves only on a network both containers join."""
    engine_networks = set(services[_ENGINE_SERVICE]["networks"])

    assert engine_networks & set(services[app]["networks"])
