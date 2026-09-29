#  Project:      dfe-docker
#  File:         tests/test_hyperdx_memory.py
#  Purpose:      Assert HyperDX's memory ceiling covers what the image needs to start
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""HyperDX's memory ceiling on Compose.

The dfe-hyperdx image runs four Node processes. On node 24 the container holds
about 820 MiB for its first 100 s after a start and then settles near 560 MiB, so
the service tier's 768M OOM-kills it before it settles and it restarts in a loop.
The console's search page never loads while it does.

Read off the interpolated model `docker compose config` produces, for the reason
check_compose gives.
"""

import shutil

import pytest

import check_compose

_HYPERDX_SERVICE = "hyperdx"
_ENGINE_SERVICE = "dfe-engine"
_MEMORY_KEYS = ("DFE_HYPERDX_MEMORY", "DFE_SERVICE_MEMORY")
# The startup peak, with room for the queries a search runs while it lasts.
_MIN_HYPERDX_BYTES = 1024 * 2**20


def _memory_limits(**environment: str) -> dict[str, int]:
    """Return {service: memory limit in bytes} for the registry path."""
    if shutil.which("docker") is None:
        pytest.skip("`docker compose config` renders the model these assert on")
    env, _ = check_compose._check_env()
    for key in _MEMORY_KEYS:
        env.pop(key, None)
    env.update(environment)
    config = check_compose._config_json(
        env=env, files=[check_compose.COMPOSE_FILE.name]
    )
    assert config is not None, "the registry compose path did not resolve"
    return {
        name: int(service["deploy"]["resources"]["limits"]["memory"])
        for name, service in config["services"].items()
        if name in (_HYPERDX_SERVICE, _ENGINE_SERVICE)
    }


def test_hyperdx_has_room_for_its_startup_peak() -> None:
    assert _memory_limits()[_HYPERDX_SERVICE] >= _MIN_HYPERDX_BYTES


def test_the_hyperdx_ceiling_moves_alone() -> None:
    """Raising HyperDX's ceiling leaves every other service where it was."""
    default = _memory_limits()
    raised = _memory_limits(DFE_HYPERDX_MEMORY="3G")

    assert raised[_HYPERDX_SERVICE] == 3 * 2**30
    assert raised[_ENGINE_SERVICE] == default[_ENGINE_SERVICE]


def test_the_service_tier_no_longer_sets_hyperdx_memory() -> None:
    """An operator trimming the service tier must not take HyperDX under its peak."""
    trimmed = _memory_limits(DFE_SERVICE_MEMORY="512M")

    assert trimmed[_ENGINE_SERVICE] == 512 * 2**20
    assert trimmed[_HYPERDX_SERVICE] >= _MIN_HYPERDX_BYTES
