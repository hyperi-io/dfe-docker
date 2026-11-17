#  Project:      dfe-docker
#  File:         scripts/tests/test_demo_theatre_churn.py
#  Purpose:      Run the theatre churn dashboard's "dropped" and "back" tile queries on the stack's ClickHouse over seeded tracks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The two churn tiles that verify.json lists in zero_ok, run against rows that must fill them.

demo_verify cannot require rows from `demo-theatre-churn/dropped` and `demo-theatre-churn/back`, because a live feed gives a CI run no track that goes silent or comes back. So a tile whose SQL is wrong would pass there. This test seeds tracks into `dfe.adsb-tracks` and `dfe.ais-tracks` and reads the tile's real SQL, with the console's macros expanded by demo_verify, on the ClickHouse image the stack pins.

The seeded tracks, per feed:

  back     reports every minute, goes silent for ten minutes, then reports every minute to now
  dropped  reports every minute, then stops 29 minutes ago
  steady   reports every minute to now
  short    a four minute gap, under the five that counts as silent
  recent   reports every minute, then stops six minutes ago, under the seven that counts as dropped

`DFE_CLICKHOUSE_VERSION` names the image tag (tag@digest), else scripts/stack.py reads the pin the stack carries, which needs `oras` on PATH. Without docker the test skips, except under CI, where it fails.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import demo_verify
from _common import demo_pack_dir

ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD = demo_pack_dir(pack="theatre") / "console/dashboards/demo-theatre-churn.json"
IMAGE_REPO = "clickhouse/clickhouse-server"

# Tables as the loader makes them, as far as the tiles read: the event time and the raw payload
CREATE = """
CREATE DATABASE dfe;
CREATE TABLE dfe.`adsb-tracks` (`_timestamp` DateTime64(3, 'UTC'), `_json` JSON) ENGINE = Memory;
CREATE TABLE dfe.`ais-tracks` (`_timestamp` DateTime64(3, 'UTC'), `_json` JSON) ENGINE = Memory;
"""

# Minutes ago of each report, per seeded track, one report a minute except across the gaps
TRACKS = {
    "back": [*range(40, 37, -1), *range(28, -1, -1)],
    "dropped": list(range(40, 28, -1)),
    "steady": list(range(40, -1, -1)),
    "short": [*range(40, 36, -1), *range(33, -1, -1)],
    "recent": list(range(40, 5, -1)),
}

# What each tile must list, by the feed and the name the tile shows. An air track shows its callsign, and a sea track its MMSI.
AIR_NAME = {
    "back": "BACK1",
    "dropped": "DROP1",
    "steady": "STDY1",
    "short": "SHRT1",
    "recent": "RCNT1",
}
SEA_NAME = {
    "back": "230000001",
    "dropped": "230000002",
    "steady": "230000003",
    "short": "230000004",
    "recent": "230000005",
}
EXPECTED = {
    "dropped": {("air", AIR_NAME["dropped"]), ("sea", SEA_NAME["dropped"])},
    "back": {("air", AIR_NAME["back"]), ("sea", SEA_NAME["back"])},
}

# A tile edited the way a regression would: the silent threshold or the dropped threshold moved out of reach of the seeded tracks
BROKEN = {
    "dropped": ("INTERVAL 7 MINUTE", "INTERVAL 70 MINUTE"),
    "back": (">= 300", ">= 3000"),
}


def row_sql(*, table: str, minutes_ago: int, payload: dict) -> str:
    """One INSERT row, `minutes_ago` before now, whose payload is the JSON the loader stores in `_json`."""
    text = json.dumps(payload).replace("\\", "\\\\").replace("'", "\\'")
    return (
        f"INSERT INTO dfe.`{table}` VALUES "
        f"(now64(3, 'UTC') - INTERVAL {minutes_ago * 60} SECOND, '{text}'::JSON);"
    )


def seed_sql() -> str:
    statements = []
    for kind, minutes in TRACKS.items():
        for ago in minutes:
            statements.append(
                row_sql(
                    table="adsb-tracks",
                    minutes_ago=ago,
                    payload={
                        "hex": f"a{list(TRACKS).index(kind)}0000",
                        "flight": AIR_NAME[kind],
                    },
                )
            )
            statements.append(
                row_sql(
                    table="ais-tracks",
                    minutes_ago=ago,
                    payload={
                        "mmsi": int(SEA_NAME[kind]),
                        "geometry": {"coordinates": [24.9, 60.1]},
                        "properties": {"cog": 90.0, "sog": 5.5, "navStat": 0},
                    },
                )
            )
    return "\n".join(statements)


def tile_sql(*, tile: str) -> str:
    """A churn tile's SQL as the dashboard stores it."""
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    (found,) = [item for item in dashboard["tiles"] if item["id"] == tile]
    return found["config"]["sqlTemplate"]


def clickhouse_image() -> str:
    pin = os.environ.get("DFE_CLICKHOUSE_VERSION", "").strip()
    if not pin:
        done = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/stack.py"),
                "--print",
                "CLICKHOUSE_VERSION",
            ],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        assert done.returncode == 0, (
            f"stack.py could not read the ClickHouse pin: {done.stderr or done.stdout}"
        )
        pin = done.stdout.strip().splitlines()[-1]
    return f"{IMAGE_REPO}:{pin}"


@pytest.fixture(scope="module")
def image() -> str:
    if shutil.which("docker") is None:
        if os.environ.get("CI"):
            pytest.fail("docker is not on PATH, so the churn tiles ran on nothing")
        pytest.skip("docker is not on PATH")
    return clickhouse_image()


def run_tile(*, image: str, sql: str) -> list[dict]:
    """The rows `sql` returns over the seeded tables, run on clickhouse-local from the stack's ClickHouse image."""
    script = "\n".join([CREATE, seed_sql(), demo_verify.expand_macros(sql=sql) + ";"])
    done = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--entrypoint",
            "clickhouse",
            image,
            "local",
            "--multiquery",
            "--output-format",
            "JSONEachRow",
        ],
        input=script,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert done.returncode == 0, f"clickhouse local failed: {done.stderr}"
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("tile", ["dropped", "back"])
def test_tile_lists_exactly_the_seeded_tracks(image: str, tile: str) -> None:
    rows = run_tile(image=image, sql=tile_sql(tile=tile))
    assert {(row["Feed"], row["Track"]) for row in rows} == EXPECTED[tile]
    if tile == "back":
        assert {row["Silent (min)"] for row in rows} == {10}


@pytest.mark.parametrize("tile", ["dropped", "back"])
def test_a_broken_tile_is_caught(image: str, tile: str) -> None:
    old, new = BROKEN[tile]
    sql = tile_sql(tile=tile)
    assert old in sql
    rows = run_tile(image=image, sql=sql.replace(old, new))
    assert {(row["Feed"], row["Track"]) for row in rows} != EXPECTED[tile]
