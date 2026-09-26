#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         e2e_posture.py
#  Purpose:      Put .env into the posture the dfe-ui Playwright suite runs against
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Declare an e2e stack in .env: the one the dfe-ui Playwright suite drives.

The suite seeds its fixtures through the engine's /api/e2e routes, which the
engine mounts only with DFE_E2E_SERVER on and a non-production posture, and whose
seeders refuse anything but DFE_ENV=test. It addresses containers as `e2e-<service>`
and the receiver and loader on the 2xxxx ports, so the stack gets that prefix and a
port family of its own, and can run beside a default stack on one daemon.

It names no compose project. The checkout's directory names it, so its volumes
are its own, and a second checkout in this posture fails on the container names
rather than sharing, or tearing down, the first one's stack.

The admin password is set to the shipped default the suite's reset returns it to,
so the same refusal `make dev` has applies: a .env whose DFE_ENV names a
deployment is refused, exit 2, and the file it rewrites is backed up first.
"""

from __future__ import annotations

import sys

from _common import DOTENV_FILE, _dotenv_values, _print, _rel_path
from creds import _ADMIN_PASSWORD_KEY, _DEFAULT_PASSWORD, is_dev_posture
from dev_posture import _backup, _rewrite

_POSTURE_KEY = "DFE_ENV"
_REFUSED = 2

SETTINGS = {
    _POSTURE_KEY: "test",
    _ADMIN_PASSWORD_KEY: _DEFAULT_PASSWORD,
    "DFE_E2E_SERVER": "true",
    "DFE_CONTAINER_PREFIX": "e2e-",
}

# Every host port the stack publishes, moved into one family. The receiver and
# loader ones are the suite's own defaults; the UI and engine ones it is told.
PORTS = {
    "CLICKHOUSE_HTTP_HOST_PORT": "28123",
    "CLICKHOUSE_NATIVE_HOST_PORT": "29000",
    "DFE_ARCHIVER_PROMETHEUS_PORT": "29093",
    "DFE_ENGINE_PORT": "28003",
    "DFE_FETCHER_INGEST_PORT": "28082",
    "DFE_FETCHER_PROMETHEUS_PORT": "29094",
    "DFE_HYPERDX_API_PORT": "28000",
    "DFE_HYPERDX_APP_PORT": "28090",
    "DFE_HYPERDX_EMBED_PORT": "28091",
    "DFE_LOADER_GRPC_PORT": "50151",
    "DFE_LOADER_PROMETHEUS_PORT": "29091",
    "DFE_OTEL_FLUENT_PORT": "23224",
    "DFE_OTEL_HEALTH_PORT": "23133",
    "DFE_RECEIVER_GRPC_PORT": "26000",
    "DFE_RECEIVER_HTTP_PORT": "28080",
    "DFE_RECEIVER_PROMETHEUS_PORT": "29090",
    "DFE_TRANSFORM_ELASTIC_CISCO_IOS_PROMETHEUS_PORT": "29089",
    "DFE_TRANSFORM_ELASTIC_PROMETHEUS_PORT": "29099",
    "DFE_TRANSFORM_VECTOR_FILEBEAT_PROMETHEUS_PORT": "29098",
    "DFE_TRANSFORM_VECTOR_PROMETHEUS_PORT": "29095",
    "DFE_TRANSFORM_VRL_FILEBEAT_PROMETHEUS_PORT": "29097",
    "DFE_TRANSFORM_VRL_PROMETHEUS_PORT": "29096",
    "DFE_UI_PORT": "23000",
    "KAFBAT_PORT": "28081",
    "KAFKA_PLAINTEXT_HOST_PORT": "39092",
    "KAFKA_PLAINTEXT_PORT": "29092",
}

BANNER = "## Written by `make e2e-posture` - the dfe-ui Playwright suite's stack, not a deployment."


def main() -> int:
    if not (DOTENV_FILE.is_file()):
        _print(
            header=_rel_path(path=DOTENV_FILE), msg="Missing -- run `make init` first"
        )
        return 1

    values = _dotenv_values()
    environment = values.get(_POSTURE_KEY, "").strip()
    if environment and not (is_dev_posture(environment)):
        _print(
            header=_rel_path(path=DOTENV_FILE),
            msg=f"DFE_ENV={environment} is not a dev posture, so `make e2e-posture` "
            "refuses: it would overwrite this deployment's admin password with the "
            "shipped default. Run the suite from a checkout of its own.",
        )
        return _REFUSED

    text = DOTENV_FILE.read_text(encoding="utf-8")
    rewritten = _rewrite(banner=BANNER, text=text, wanted={**SETTINGS, **PORTS})
    if rewritten == text:
        _print(header=_rel_path(path=DOTENV_FILE), msg="E2E posture already set")
        return 0

    backup = _backup(text=text)
    with DOTENV_FILE.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rewritten)
    _print(
        header=_rel_path(path=DOTENV_FILE),
        msg=f"E2E posture: DFE_ENV=test, the engine's e2e routes on, containers "
        f"prefixed e2e-, UI on :{PORTS['DFE_UI_PORT']} and engine on "
        f":{PORTS['DFE_ENGINE_PORT']}. The file it replaced is "
        f"{_rel_path(path=backup)}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
