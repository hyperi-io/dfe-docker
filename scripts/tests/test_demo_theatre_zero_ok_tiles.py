#  Project:      dfe-docker
#  File:         scripts/tests/test_demo_theatre_zero_ok_tiles.py
#  Purpose:      Run the theatre tiles verify.json lists in zero_ok on the stack's ClickHouse over seeded tracks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The theatre tiles that verify.json lists in zero_ok, run against rows that must fill them.

demo_verify cannot require rows from `demo-theatre-churn/dropped`, `demo-theatre-churn/back` or `demo-theatre-vessel-moves/movers`, because a live feed may give a CI run no track that goes silent, comes back, or turns. So a tile whose SQL is wrong would pass there. This test seeds tracks into `dfe.adsb-tracks` and `dfe.ais-tracks` and reads the tile's real SQL, with the console's macros expanded by demo_verify, on the ClickHouse image the stack pins.

The seeded tracks, per feed:

  back     reports every minute, goes silent for ten minutes, then reports every minute to now
  dropped  reports every minute, then stops 29 minutes ago
  steady   reports every minute to now, and at sea turns 30 degrees between reports
  short    a four minute gap, under the five that counts as silent
  recent   reports every minute, then stops six minutes ago, under the seven that counts as dropped
  speeding reports every minute to now, and at sea changes speed by 2.5 knots between reports

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
DASHBOARDS = demo_pack_dir(pack="theatre") / "console/dashboards"
# The dashboard each tested tile sits on
TILE_DASHBOARD = {
    "dropped": "demo-theatre-churn",
    "back": "demo-theatre-churn",
    "movers": "demo-theatre-vessel-moves",
}
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
    "speeding": list(range(40, -1, -1)),
}

# What each tile must list, by the feed and the name the tile shows. An air track shows its callsign, and a sea track its MMSI.
AIR_NAME = {
    "back": "BACK1",
    "dropped": "DROP1",
    "steady": "STDY1",
    "short": "SHRT1",
    "recent": "RCNT1",
    "speeding": "SPED1",
}
SEA_NAME = {
    "back": "230000001",
    "dropped": "230000002",
    "steady": "230000003",
    "short": "230000004",
    "recent": "230000005",
    "speeding": "230000006",
}
EXPECTED = {
    "dropped": {("air", AIR_NAME["dropped"]), ("sea", SEA_NAME["dropped"])},
    "back": {("air", AIR_NAME["back"]), ("sea", SEA_NAME["back"])},
    "movers": {("sea", SEA_NAME["steady"]), ("sea", SEA_NAME["speeding"])},
}
# The sea tracks whose course swings 30 degrees, and whose speed swings 2.5 knots, between reports
TURNING = {"steady"}
SPEEDING = {"speeding"}

# A tile edited the way a regression would: a threshold moved out of reach of the seeded tracks
# (tile, text, its broken replacement, the track that must then drop out of the tile)
BROKEN = [
    ("dropped", "INTERVAL 7 MINUTE", "INTERVAL 70 MINUTE", SEA_NAME["dropped"]),
    ("back", ">= 300", ">= 3000", SEA_NAME["back"]),
    ("movers", "course_change >= 10", "course_change >= 100", SEA_NAME["steady"]),
    (
        "movers",
        "abs(speed_change) >= 1)",
        "abs(speed_change) >= 10)",
        SEA_NAME["speeding"],
    ),
]


def listed(*, tile: str, rows: list[dict]) -> set[tuple[str, str]]:
    """The (feed, name) pairs a tile's rows list. The movers tile is sea-only and names its track by MMSI."""
    if tile == "movers":
        return {("sea", row["MMSI"]) for row in rows}
    return {(row["Feed"], row["Track"]) for row in rows}


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
                        "properties": {
                            "cog": 120.0 if kind in TURNING and ago % 2 else 90.0,
                            "sog": 8.0 if kind in SPEEDING and ago % 2 else 5.5,
                            "navStat": 0,
                        },
                    },
                )
            )
    return "\n".join(statements)


def tile_sql(*, tile: str) -> str:
    """A tile's SQL as its dashboard stores it."""
    path = DASHBOARDS / f"{TILE_DASHBOARD[tile]}.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
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
            pytest.fail("docker is not on PATH, so the zero_ok tiles ran on nothing")
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


@pytest.mark.parametrize("tile", ["dropped", "back", "movers"])
def test_tile_lists_exactly_the_seeded_tracks(image: str, tile: str) -> None:
    rows = run_tile(image=image, sql=tile_sql(tile=tile))
    assert listed(tile=tile, rows=rows) == EXPECTED[tile]
    if tile == "back":
        assert {row["Silent (min)"] for row in rows} == {10}


@pytest.mark.parametrize(("tile", "old", "new", "gone"), BROKEN)
def test_a_broken_tile_is_caught(
    image: str, tile: str, old: str, new: str, gone: str
) -> None:
    sql = tile_sql(tile=tile)
    assert old in sql
    rows = run_tile(image=image, sql=sql.replace(old, new))
    assert ("sea", gone) not in listed(tile=tile, rows=rows)
