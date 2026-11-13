#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resources/demo-telco/events/generate.py
#  Purpose:      Regenerate the telco pack's cell KPI counters and core network probe records
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Write ran-kpis/demo.log and core-probes/demo.log beside this file, one JSON record a line, one directory per pack source, or replay them at the receiver.

  generate.py
  generate.py replay --target http://dfe-receiver:8080/ingest --sources ran-kpis,core-probes --loop

Seeded, so a rerun writes the same bytes. Each file covers one generated day of 1,440 minutes, 21:00 to 21:00 UTC on the clock its load curve follows, so it ends on the evening busy hour. The replay shifts the day to end at the moment it starts, so minute 1440 is "now" and the last hour on the console holds every fault below.

ran-kpis is one record per cell per five-minute reporting period: 20 cells on 7 sites, each with downlink and uplink throughput, average RSRP, downlink PRB utilisation and active users, all following a daily load curve (quiet at 04:00, a morning ramp, a busy hour near 20:00 on the residential and airport sites, midday in the CBD). core-probes is about 6,000 probe records over the same day on the same curve: NGAP (N2), GTPv2-C (S11) and Diameter (S6a, Gx) procedures, each with its result, cause code and latency.

The replay moves every `timestamp` so the newest record lands now and sends the whole day behind it at once, so the console is full from its first screen. With --loop it then goes round again a period later, the day's span rounded up to whole hours, sending each record as its time comes. Standard library only, because it runs in the stack's bare Python image.

The faults, each a dashboard's story, by minute of the generated day:

  minute 1380 (20:00) to 1415  RSRP drop on NTH03-B: 13 dB down, as a failed antenna feeder does. Its users move to NTH03-A and NTH03-C and its throughput falls with the signal.
  minute 1395 (20:15) to 1425  Throughput collapse on AIR07-A: the radio is healthy and the users stay, but the backhaul carries a tenth of the traffic, so PRB utilisation falls with it.
  minute 1405 (20:25) to 1415  CBD01-C goes silent: no counters for two reporting periods. Its users land on CBD01-A and CBD01-B.
  minute 1406 (20:26) to 1418  S6a Update-Location failure burst: the users CBD01-C dropped register again at once, and the HSS answers DIAMETER_TOO_BUSY (3004) or nothing, DIAMETER_UNABLE_TO_DELIVER (3002), with latency up twentyfold.
"""

import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261116
# Minute 0 of the generated day, on the clock the load curve follows
START = datetime(2026, 11, 15, 21, tzinfo=UTC)
DAY_MINUTES = 1440
PERIOD_MINUTES = 5
PROBE_RECORDS = 6000
SOURCE_FIELD = "_dfe_demo_source"
SOURCES = ("ran-kpis", "core-probes")
# Records a POST carries, inside the receiver's batch ceiling
REPLAY_BATCH = 500
REPLAY_TIMEOUT_SECONDS = 30.0
RETRY_SECONDS = 5.0
HOUR_SECONDS = 3600.0

# (cell, site, rat, band, busy-hour users, peak downlink Mbps, cell RSRP dBm, load profile)
CELLS = [
    ("CBD01-A", "CBD01", "NR", "n78", 210, 1150.0, -86.0, "office"),
    ("CBD01-B", "CBD01", "NR", "n78", 185, 1150.0, -88.5, "office"),
    ("CBD01-C", "CBD01", "NR", "n78", 160, 1150.0, -90.0, "office"),
    ("CBD02-A", "CBD02", "NR", "n78", 230, 1150.0, -85.5, "office"),
    ("CBD02-B", "CBD02", "NR", "n78", 175, 1150.0, -89.0, "office"),
    ("CBD02-C", "CBD02", "NR", "n78", 150, 1150.0, -91.5, "office"),
    ("NTH03-A", "NTH03", "LTE", "B3", 120, 150.0, -93.0, "home"),
    ("NTH03-B", "NTH03", "LTE", "B3", 135, 150.0, -92.0, "home"),
    ("NTH03-C", "NTH03", "LTE", "B3", 110, 150.0, -95.0, "home"),
    ("STH04-A", "STH04", "LTE", "B3", 125, 150.0, -94.0, "home"),
    ("STH04-B", "STH04", "LTE", "B3", 115, 150.0, -96.5, "home"),
    ("STH04-C", "STH04", "LTE", "B3", 100, 150.0, -97.0, "home"),
    ("WST05-A", "WST05", "LTE", "B28", 140, 110.0, -98.0, "home"),
    ("WST05-B", "WST05", "LTE", "B28", 125, 110.0, -99.5, "home"),
    ("EST06-A", "EST06", "LTE", "B3", 105, 150.0, -95.5, "home"),
    ("EST06-B", "EST06", "LTE", "B3", 95, 150.0, -96.0, "home"),
    ("EST06-C", "EST06", "LTE", "B3", 90, 150.0, -97.5, "home"),
    ("AIR07-A", "AIR07", "NR", "n78", 170, 1150.0, -87.0, "airport"),
    ("AIR07-B", "AIR07", "NR", "n78", 155, 1150.0, -88.0, "airport"),
    ("AIR07-C", "AIR07", "NR", "n78", 145, 1150.0, -89.5, "airport"),
]

RSRP_DROP = ("NTH03-B", 1380, 1415)
BACKHAUL = ("AIR07-A", 1395, 1425)
SILENT = ("CBD01-C", 1405, 1415)
HSS_BURST = (1406, 1418)

# (protocol, interface, procedure, share of records, median latency ms, baseline failure rate, failure causes as (code, name, weight))
PROCEDURES = [
    (
        "NGAP",
        "N2",
        "InitialUEMessage",
        0.13,
        38.0,
        0.006,
        [
            ("radioNetwork", "radio-connection-with-ue-lost", 3),
            ("nas", "authentication-failure", 2),
            ("misc", "not-enough-user-plane-processing-resources", 1),
        ],
    ),
    (
        "NGAP",
        "N2",
        "PDUSessionResourceSetup",
        0.12,
        72.0,
        0.008,
        [
            ("radioNetwork", "radio-resources-not-available", 3),
            ("transport", "transport-resource-unavailable", 2),
            ("protocol", "semantic-error", 1),
        ],
    ),
    (
        "NGAP",
        "N2",
        "HandoverPreparation",
        0.08,
        27.0,
        0.015,
        [
            ("radioNetwork", "ho-target-not-allowed", 2),
            ("radioNetwork", "ho-failure-in-target-5GC-ngran-node-or-target-system", 3),
            ("radioNetwork", "tngrelocprep-expiry", 1),
        ],
    ),
    (
        "GTPv2-C",
        "S11",
        "CreateSession",
        0.14,
        16.0,
        0.007,
        [
            ("73", "No resources available", 2),
            ("72", "System failure", 1),
            ("94", "Request rejected (reason not specified)", 2),
        ],
    ),
    (
        "GTPv2-C",
        "S11",
        "ModifyBearer",
        0.13,
        9.0,
        0.004,
        [
            ("64", "Context Not Found", 3),
            (
                "110",
                "Temporarily rejected due to handover/TAU/RAU procedure in progress",
                2,
            ),
        ],
    ),
    (
        "GTPv2-C",
        "S11",
        "DeleteSession",
        0.08,
        7.0,
        0.003,
        [("64", "Context Not Found", 4), ("72", "System failure", 1)],
    ),
    (
        "Diameter",
        "S6a",
        "Update-Location",
        0.12,
        12.0,
        0.005,
        [
            ("5001", "DIAMETER_ERROR_USER_UNKNOWN", 2),
            ("5420", "DIAMETER_ERROR_UNKNOWN_EPS_SUBSCRIPTION", 2),
            ("5012", "DIAMETER_UNABLE_TO_COMPLY", 1),
        ],
    ),
    (
        "Diameter",
        "S6a",
        "Authentication-Information",
        0.11,
        9.0,
        0.004,
        [
            ("4181", "DIAMETER_AUTHENTICATION_DATA_UNAVAILABLE", 2),
            ("5001", "DIAMETER_ERROR_USER_UNKNOWN", 1),
        ],
    ),
    (
        "Diameter",
        "Gx",
        "Credit-Control-Initial",
        0.09,
        11.0,
        0.004,
        [("5012", "DIAMETER_UNABLE_TO_COMPLY", 2), ("3004", "DIAMETER_TOO_BUSY", 1)],
    ),
]
SUCCESS = {
    "NGAP": (None, None),
    "GTPv2-C": ("16", "Request accepted"),
    "Diameter": ("2001", "DIAMETER_SUCCESS"),
}
# The peer each procedure runs between, so a node pair reads as a real core does
PEERS = {
    "N2": ("gnb", "amf-01"),
    "S11": ("mme-01", "sgw-01"),
    "S6a": ("mme-01", "hss-01"),
    "Gx": ("pgw-01", "pcrf-01"),
}
TIMEOUT_MS = 3000.0


def _bump(*, centre: float, hour: float, width: float) -> float:
    """A smooth hump of height 1 at `centre`, `width` hours wide, on a 24-hour clock."""
    distance = min(abs(hour - centre), 24.0 - abs(hour - centre))
    return math.exp(-0.5 * (distance / width) ** 2)


def load(*, minute: float, profile: str) -> float:
    """The share of its busy-hour load a cell carries at one minute of the generated day, 0.06 at the quietest and 1.0 at the peak."""
    hour = (START.hour + minute / 60.0) % 24.0
    if profile == "office":
        shape = (
            0.55 * _bump(centre=8.6, hour=hour, width=1.2)
            + 1.0 * _bump(centre=12.8, hour=hour, width=2.6)
            + 0.7 * _bump(centre=17.6, hour=hour, width=1.4)
        )
    elif profile == "airport":
        shape = (
            0.8 * _bump(centre=7.2, hour=hour, width=1.5)
            + 0.75 * _bump(centre=13.5, hour=hour, width=2.0)
            + 1.0 * _bump(centre=19.0, hour=hour, width=2.2)
        )
    else:
        shape = (
            0.45 * _bump(centre=8.0, hour=hour, width=1.3)
            + 0.5 * _bump(centre=13.0, hour=hour, width=2.5)
            + 1.0 * _bump(centre=20.4, hour=hour, width=2.1)
        )
    return 0.06 + 0.94 * min(1.0, shape)


def _stamp(*, moment: datetime) -> str:
    """An RFC 3339 UTC timestamp to the millisecond, the shape the loader reads into `_timestamp`."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _in(*, window: tuple[int, int], minute: int) -> bool:
    """Whether a minute of the day falls inside a fault's [start, end) window."""
    return window[0] <= minute < window[1]


def _kpi(
    *, cell: tuple, minute: int, rng: random.Random, shares: dict[str, float]
) -> dict:
    """One cell's counters for the five-minute period starting at `minute`."""
    name, site, rat, band, peak_users, peak_mbps, rsrp_base, profile = cell
    level = load(minute=minute, profile=profile) * (1.0 + rng.gauss(0.0, 0.04))
    users = max(0.0, peak_users * level * shares.get(name, 1.0))
    rsrp = rsrp_base - 1.2 * (1.0 - level) + rng.gauss(0.0, 0.5)
    if _in(window=RSRP_DROP[1:], minute=minute) and name == RSRP_DROP[0]:
        rsrp -= 13.0
    prb = min(
        99.0,
        max(0.5, 100.0 * min(1.0, users / peak_users) * 0.86 + rng.gauss(0.0, 1.2)),
    )
    # Spectral efficiency falls with the signal, so the same PRBs carry less when RSRP drops
    efficiency = min(1.0, max(0.15, (rsrp + 122.0) / 34.0))
    downlink = peak_mbps * prb / 100.0 * efficiency * (1.0 + rng.gauss(0.0, 0.03))
    if _in(window=BACKHAUL[1:], minute=minute) and name == BACKHAUL[0]:
        downlink *= 0.1
        prb = max(0.5, prb * 0.18)
    uplink = downlink * (0.11 + rng.gauss(0.0, 0.01))
    start = START + timedelta(minutes=minute)
    return {
        "timestamp": _stamp(moment=start),
        SOURCE_FIELD: "ran-kpis",
        "cell": name,
        "site": site,
        "rat": rat,
        "band": band,
        "period_secs": PERIOD_MINUTES * 60,
        "active_users": round(users),
        "prb_util_dl_pct": round(prb, 1),
        "rsrp_dbm": round(rsrp, 1),
        "dl_throughput_mbps": round(max(0.0, downlink), 1),
        "ul_throughput_mbps": round(max(0.0, uplink), 1),
    }


def _shares(*, minute: int) -> dict[str, float]:
    """Each cell's share of its own users at one minute, moved onto its site neighbours while a fault holds it."""
    shares = {}
    if _in(window=RSRP_DROP[1:], minute=minute):
        shares.update({"NTH03-B": 0.7, "NTH03-A": 1.15, "NTH03-C": 1.15})
    if _in(window=SILENT[1:], minute=minute):
        shares.update({"CBD01-A": 1.3, "CBD01-B": 1.3})
    return shares


def ran_kpis(*, rng: random.Random) -> list[dict]:
    """The day's counters, every cell for every five-minute period bar the ones the silent cell misses."""
    records = []
    for minute in range(0, DAY_MINUTES, PERIOD_MINUTES):
        shares = _shares(minute=minute)
        for cell in CELLS:
            if cell[0] == SILENT[0] and _in(window=SILENT[1:], minute=minute):
                continue
            records.append(_kpi(cell=cell, minute=minute, rng=rng, shares=shares))
    return records


def _poisson(*, mean: float, rng: random.Random) -> int:
    """A Poisson count, by Knuth's product of uniforms, which is exact and quick at the small means used here."""
    limit = math.exp(-mean)
    count = 0
    product = rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def _probe(*, minute: int, procedure: tuple, rng: random.Random, level: float) -> dict:
    """One probe record of `procedure` in the minute starting at `minute`."""
    protocol, interface, name, _, median, failure_rate, causes = procedure
    burst = name == "Update-Location" and _in(window=HSS_BURST, minute=minute)
    latency = median * (1.0 + 0.35 * level) * math.exp(rng.gauss(0.0, 0.32))
    failed = rng.random() < (0.55 if burst else failure_rate)
    if burst:
        latency *= 20.0
    code, cause = SUCCESS[protocol]
    if failed:
        if burst:
            code, cause = rng.choices(
                [("3004", "DIAMETER_TOO_BUSY"), ("3002", "DIAMETER_UNABLE_TO_DELIVER")],
                weights=[3, 2],
            )[0]
            if code == "3002":
                latency = TIMEOUT_MS
        else:
            code, cause, _ = rng.choices(
                causes, weights=[weight for *_, weight in causes]
            )[0]
    source, target = PEERS[interface]
    cell = rng.choice(CELLS)[0]
    while cell == SILENT[0] and _in(window=SILENT[1:], minute=minute):
        cell = rng.choice(CELLS)[0]
    moment = START + timedelta(minutes=minute, milliseconds=rng.randrange(60_000))
    return {
        "timestamp": _stamp(moment=moment),
        SOURCE_FIELD: "core-probes",
        "protocol": protocol,
        "interface": interface,
        "procedure": name,
        "result": "failure" if failed else "success",
        "cause_code": code,
        "cause": cause,
        "latency_ms": round(min(TIMEOUT_MS, latency), 2),
        "source_node": f"gnb-{cell[:5].lower()}" if source == "gnb" else source,
        "target_node": target,
        "cell": cell if interface == "N2" else None,
    }


def core_probes(*, rng: random.Random) -> list[dict]:
    """About PROBE_RECORDS probe records spread over the day on the home-network curve, plus the registration storm the silent cell causes."""
    weights = [load(minute=minute, profile="home") for minute in range(DAY_MINUTES)]
    scale = PROBE_RECORDS / sum(weights)
    shares = [procedure[3] for procedure in PROCEDURES]
    by_name = {procedure[2]: procedure for procedure in PROCEDURES}
    records = []
    for minute, level in enumerate(weights):
        picks = []
        for _ in range(_poisson(mean=scale * level, rng=rng)):
            picks.append(rng.choices(PROCEDURES, weights=shares)[0])
        # The silent cell's users register again elsewhere, so Update-Location and InitialUEMessage climb for its first minutes
        if _in(window=HSS_BURST, minute=minute):
            storm = _poisson(mean=4.0, rng=rng)
            picks += [by_name["Update-Location"]] * storm + [
                by_name["InitialUEMessage"]
            ] * (storm // 2)
        minute_records = [
            _probe(minute=minute, procedure=procedure, rng=rng, level=level)
            for procedure in picks
        ]
        records += sorted(minute_records, key=lambda record: record["timestamp"])
    return records


def write_all() -> dict[str, int]:
    """Write every source's demo.log and return the records each holds."""
    rng = random.Random(SEED)
    written = {}
    for source, records in (
        ("ran-kpis", ran_kpis(rng=rng)),
        ("core-probes", core_probes(rng=rng)),
    ):
        path = HERE / source / "demo.log"
        path.parent.mkdir(exist_ok=True)
        lines = [json.dumps(record, separators=(",", ":")) for record in records]
        path.write_text(data="\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        written[source] = len(lines)
    return written


def read_records(
    *, sources: Sequence[str], root: Path = HERE
) -> list[tuple[float, dict]]:
    """Every record of the named sources as (epoch seconds of its `timestamp`, record), oldest first."""
    records = []
    for source in sources:
        if source not in SOURCES:
            raise ValueError(
                f"{source!r} is not a source this pack replays. Pick from {list(SOURCES)}"
            )
        path = root / source / "demo.log"
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not (line.strip()):
                continue
            try:
                record = json.loads(line)
                when = datetime.fromisoformat(str(record["timestamp"]))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{source}/demo.log:{number} has no RFC 3339 timestamp: {error}"
                ) from error
            if when.tzinfo is None:
                raise ValueError(
                    f"{source}/demo.log:{number} has a timestamp with no offset"
                )
            records.append((when.timestamp(), record))
    if not (records):
        raise ValueError(f"{list(sources)} hold no records to replay")
    return sorted(records, key=lambda item: item[0])


def round_seconds(*, times: Sequence[float]) -> float:
    """How far apart two rounds of the replay start: the records' span rounded up to whole hours, one hour at least."""
    span = max(times) - min(times)
    return max(HOUR_SECONDS, math.ceil(span / HOUR_SECONDS) * HOUR_SECONDS)


def stamped(*, record: dict, when: float) -> dict:
    """`record` with its `timestamp` set to `when`."""
    return {**record, "timestamp": _stamp(moment=datetime.fromtimestamp(when, tz=UTC))}


def _post(*, batch: Sequence[dict], target: str) -> None:
    """POST one batch as a JSON array, which dfe-receiver splits per element, retrying every RETRY_SECONDS until it takes it."""
    body = json.dumps(list(batch), separators=(",", ":")).encode("utf-8")
    failing = ""
    while True:
        request = urllib.request.Request(  # noqa: S310 - the target is the stack's own receiver
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
            url=target,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REPLAY_TIMEOUT_SECONDS
            ) as reply:  # noqa: S310
                fault = "" if 200 <= reply.status < 300 else f"HTTP {reply.status}"
        except OSError as error:
            fault = f"{type(error).__name__}: {error}"
        if not (fault):
            return
        if fault != failing:
            print(
                f"replay: {target} refused {len(batch)} records ({fault}), retrying",
                file=sys.stderr,
                flush=True,
            )
            failing = fault
        time.sleep(RETRY_SECONDS)


def replay(
    *,
    clock: Callable[[], float] = time.time,
    duration: float,
    loop: bool,
    post: Callable[..., None] = _post,
    records: Sequence[tuple[float, dict]],
    sleep: Callable[[float], None] = time.sleep,
    target: str,
) -> int:
    """Send `records` with their times moved so the newest lands now, round after round with `loop`, until `duration` seconds pass, and return how many went."""
    started = clock()
    deadline = started + duration
    offset = started - max(when for when, _ in records)
    period = round_seconds(times=[when for when, _ in records])
    sent = 0
    played = 0
    while played == 0 or loop:
        batch: list[dict] = []
        for when, record in records:
            due = when + offset + played * period
            if due > deadline:
                break
            wait = due - clock()
            if (wait > 0) or (len(batch) >= REPLAY_BATCH):
                if batch:
                    post(batch=batch, target=target)
                    sent += len(batch)
                    batch = []
                sleep(max(0.0, wait))
            batch.append(stamped(record=record, when=due))
        if batch:
            post(batch=batch, target=target)
            sent += len(batch)
        played += 1
        print(f"replay: round {played} done, {sent} records sent", flush=True)
        if records[0][0] + offset + played * period > deadline:
            break
    return sent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command")
    send = commands.add_parser(
        "replay", help="post the sources' records at dfe-receiver's HTTP ingest"
    )
    send.add_argument("--target", required=True, help="the receiver's /ingest URL")
    send.add_argument(
        "--sources", default=",".join(SOURCES), help="comma-separated sources"
    )
    send.add_argument(
        "--loop", action="store_true", help="go round again a period after each round"
    )
    send.add_argument(
        "--duration", type=float, default=600.0, help="stop after this many seconds"
    )
    args = parser.parse_args(argv)
    if args.command != "replay":
        for source, count in write_all().items():
            print(f"{source}/demo.log: {count} records")
        return 0
    try:
        records = read_records(
            sources=[name for name in args.sources.split(",") if name]
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    sent = replay(
        duration=args.duration, loop=args.loop, records=records, target=args.target
    )
    print(f"replay: sent {sent} records", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
