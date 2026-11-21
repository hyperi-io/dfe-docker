#!/usr/bin/env python3

#  Project:   dfe-docker
#  File:      scripts/test_gnmi.py
#  Purpose:   Prove gNMI telemetry lands in the otel metrics tables, against a device emulator
#  Language:  Python
#
#  License:   BUSL-1.1
#  Copyright: (c) 2026 HYPERI PTY LIMITED
#
#  Usage:
#    make up PROFILE=slim-grpc DFE_GNMIC_ENABLED=true DFE_GNMIC_CONFIG=../tests/gnmi/gnmic-emulator.yaml
#    make test-gnmi
#    make test-gnmi GNMI_ARGS=--keep

"""The gNMI acceptance test, on the running compose stack.

gnmic subscribes to a device and pushes what it reads over OTLP to the bundled
collector, which writes the otel database. This starts openconfig's lemming
device emulator on the stack's network, restarts gnmic so it subscribes to it,
and asserts fresh rows in `otel_metrics_gauge` under the `gnmic` service name.

The stack must already run gnmic on the emulator's config, as in Usage above. The
emulator is removed afterwards unless `--keep` is given.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import (  # noqa: E402
    REPO_ROOT,
    _load_dotenv,
    _print,
    _resolved_profiles,
    _resolved_services,
)
from _pipeline import ch_int, ch_query, escape_literal, poll_until  # noqa: E402

EMULATOR_FRAGMENT = REPO_ROOT / "tests" / "gnmi" / "compose.yml"
EMULATOR_SERVICE = "gnmi-emulator"
GNMIC_SERVICE = "gnmic"
# The config's metric-prefix, and the emulator's /system/processes/process leaves as gnmic names them
METRIC_PREFIX = "gnmic_"
METRIC_PATH = "system_processes_process_"
GAUGE_TABLE = "otel_metrics_gauge"
WAIT_SECONDS = 120


def _compose(*args: str) -> subprocess.CompletedProcess:
    """Run docker compose over the stack's own files and profiles plus the emulator's fragment."""
    files = [
        name
        for name in os.environ.get("COMPOSE_FILE", "docker-compose.yml").split(
            os.pathsep
        )
        if name
    ]
    cmd = ["docker", "compose"]
    for name in [*files, str(EMULATOR_FRAGMENT)]:
        cmd += ["-f", name]
    for profile in _resolved_profiles():
        cmd += ["--profile", profile]
    cmd += args
    return subprocess.run(
        cmd,
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        encoding="utf-8",
        errors="replace",
        text=True,
    )


def _landed(*, database: str, since: int) -> tuple[int, int]:
    """Count the gnmic gauge rows stamped at or after `since` (epoch seconds) and the distinct metrics in them."""
    raw = ch_query(
        f"SELECT count(), uniqExact(MetricName) FROM {database}.{GAUGE_TABLE} "
        f"WHERE ServiceName = '{escape_literal(GNMIC_SERVICE)}' "
        f"AND startsWith(MetricName, '{escape_literal(METRIC_PREFIX)}') "
        f"AND position(MetricName, '{escape_literal(METRIC_PATH)}') > 0 "
        f"AND TimeUnix >= toDateTime64({since}, 9)"
    )
    try:
        rows, metrics = (int(part) for part in raw.split())
    except ValueError:
        return 0, 0
    return rows, metrics


def _fail(message: str) -> int:
    _print(msg=f"test_gnmi: FAIL, {message}")
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep", action="store_true", help="leave the emulator running afterwards"
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    database = os.environ.get("DFE_OTEL_DATABASE", "dfe")
    if GNMIC_SERVICE not in set(_resolved_services()):
        return _fail(
            f"the resolved profile does not run {GNMIC_SERVICE}. Start the stack as in "
            "the usage note at the top of scripts/test_gnmi.py"
        )

    since = ch_int("SELECT toUnixTimestamp(now())")
    if since is None:
        return _fail("ClickHouse did not answer, so the rows cannot be read")
    started = _compose("up", "-d", "--wait", EMULATOR_SERVICE)
    try:
        if started.returncode != 0:
            return _fail(f"the emulator did not start: {started.stderr.strip()}")
        restarted = _compose("restart", GNMIC_SERVICE)
        if restarted.returncode != 0:
            return _fail(f"gnmic did not restart: {restarted.stderr.strip()}")
        rows, metrics = poll_until(
            lambda: _landed(database=database, since=since),
            done=lambda counts: counts[0] > 0,
            interval=3,
            timeout=WAIT_SECONDS,
        )
    finally:
        if not args.keep:
            _compose("rm", "--stop", "--force", EMULATOR_SERVICE)

    if rows == 0:
        return _fail(
            f"no {METRIC_PREFIX}*{METRIC_PATH}* rows landed in {database}.{GAUGE_TABLE} within "
            f"{WAIT_SECONDS}s. Read `docker compose logs {GNMIC_SERVICE} otel-collector`"
        )
    _print(
        msg=f"test_gnmi: PASS, {rows} rows across {metrics} metrics, all newer than the run, in "
        f"{database}.{GAUGE_TABLE} for ServiceName '{GNMIC_SERVICE}'"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
