#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resources/demo-cyber/events/generate.py
#  Purpose:      Regenerate the cyber demo's sample events, and replay its Beats-envelope sources at dfe-receiver
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Write <source>/demo.log beside this file for the cyber demo, or replay the Beats-envelope ones.

  generate.py --fixtures <dfe-transform-elastic>/tests/fixtures/unencumbered
  generate.py replay --target http://dfe-receiver:8080/ingest --sources okta,crowdstrike,o365 --rate 10 --loop

The firewall lines come from resources/demo/events/generate.py with a share of the outside addresses moved onto networks on the Spamhaus DROP list. okta, crowdstrike and o365 are records from the upstream samples named in PROVENANCE.md, varied per line and each wrapped in the envelope an Elastic Agent ships, routed on `data_stream.dataset`. Seeded, so a rerun with the same fixtures writes the same bytes.

The replay sets every record's own time and `@timestamp` to now as it sends, so the console's time filters find it. Standard library only, because it runs in the stack's bare Python image.
"""

import argparse
import copy
import hashlib
import importlib.util
import itertools
import json
import random
import re
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
FIREWALL_GENERATOR = HERE.parent.parent / "demo" / "events" / "generate.py"
LINES = 6000
SEED = 20261114
# Where the sample times start, stepped forward per record. The replay moves every one to now
EPOCH = datetime(2026, 11, 14, tzinfo=UTC)

# Eight of the widest networks on the Spamhaus DROP list of 2026-11-13. The cyber dashboards read the same list
DROP_NETWORKS = (
    "42.128.0.0/12",
    "42.160.0.0/12",
    "42.208.0.0/12",
    "147.16.0.0/14",
    "196.16.0.0/14",
    "101.134.0.0/15",
    "112.142.0.0/15",
    "168.80.0.0/15",
)
# Two hosts on each DROP network, which the firewalls see and which then try to sign in
SUSPECTS = tuple(
    f"{net.split('.')[0]}.{int(net.split('.')[1]) + 1}.{host}.{host * 3 % 250 + 2}"
    for net in DROP_NETWORKS
    for host in (17, 113)
)
# Each suspect's share of the firewalls' outside addresses is SUSPECT_WEIGHT in roughly 500
SUSPECT_WEIGHT = 3
STAFF_ADDRESSES = [f"203.0.113.{n}" for n in range(20, 60)]
DOMAIN = "example.com"
# Every domain the upstream samples carry, moved onto the reserved example domains, in order
DOMAIN_MAP = tuple(
    (re.compile(pattern, flags=re.IGNORECASE), replacement)
    for pattern, replacement in (
        (r"frothly-my\.sharepoint\.com", "files.example.com"),
        (r"frothly\.onmicrosoft\.com", DOMAIN),
        (r"frothly_onmicrosoft_com", "example_com"),
        (r"_froth_ly", "_example_com"),
        (r"frothly\.sharepoint\.com", "sites.example.com"),
        (r"\btheFroth\b", "allstaff"),
        (r"\bfroth\.ly\b", DOMAIN),
        (r"\b[a-z0-9]+\.prod\.outlook\.com\b", f"mail.{DOMAIN}"),
        (r"\b(springfield\.gov|emaildomain\.com|email\.com|acme\.io)\b", DOMAIN),
        (r"\b(hobbiton\.co|panther\.com)\b", DOMAIN),
        (
            r"\b(live\.com|naver\.com|isp\.net|rnvtelecom\.com\.br|sbcglobal\.net)\b",
            "example.net",
        ),
        (r"\binfo\.org\b", "example.org"),
    )
)
# The upstream tenant's name wherever it is not part of a domain
TENANT = re.compile(r"frothly", flags=re.IGNORECASE)
# Documentation ASNs (RFC 5398) for the network an Okta sign-in comes from
STAFF_NETWORK = {
    "asNumber": 64500,
    "asOrg": "Example Networks",
    "domain": "example.net",
    "isProxy": False,
    "isp": "Example ISP",
}
SUSPECT_NETWORK = {
    "asNumber": 64510,
    "asOrg": "Example Hosting",
    "domain": "example.org",
    "isProxy": True,
    "isp": "Example Hosting",
}
# (city, state, country, lat, lon, weight)
COUNTRIES = (
    ("Sydney", "New South Wales", "Australia", -33.8688, 151.2093, 40),
    ("Melbourne", "Victoria", "Australia", -37.8136, 144.9631, 25),
    ("Auckland", "Auckland", "New Zealand", -36.8485, 174.7633, 10),
    ("Singapore", "Singapore", "Singapore", 1.3521, 103.8198, 10),
    ("Seattle", "Washington", "United States", 47.6062, -122.3321, 8),
    ("London", "England", "United Kingdom", 51.5072, -0.1276, 7),
)
SUSPECT_COUNTRIES = (
    ("Bucharest", "Bucuresti", "Romania", 44.4268, 26.1025, 1),
    ("Lagos", "Lagos", "Nigeria", 6.5244, 3.3792, 1),
    ("Hong Kong", "Hong Kong", "Hong Kong", 22.3193, 114.1694, 1),
)
HOSTS = [f"WKS-SYD-{n:03d}" for n in range(1, 25)] + [
    "SRV-DB-01",
    "SRV-DB-02",
    "SRV-FILE-01",
    "SRV-WEB-03",
    "SRV-DC-01",
]
# (name, tactic, technique, technique id, objective)
DETECTIONS = (
    ("Known Malware", "Execution", "User Execution", "T1204", "Follow Through"),
    (
        "Credential Dumping",
        "Credential Access",
        "OS Credential Dumping",
        "T1003",
        "Gain Access",
    ),
    ("Suspicious PowerShell", "Execution", "PowerShell", "T1059.001", "Follow Through"),
    (
        "Ransomware Behaviour",
        "Impact",
        "Data Encrypted for Impact",
        "T1486",
        "Keep Access",
    ),
    (
        "Lateral Movement via SMB",
        "Lateral Movement",
        "SMB/Windows Admin Shares",
        "T1021.002",
        "Explore",
    ),
)
# (name, score, weight)
SEVERITIES = (
    ("Informational", 10, 30),
    ("Low", 30, 30),
    ("Medium", 50, 22),
    ("High", 70, 13),
    ("Critical", 90, 5),
)
OKTA_FAILURES = ("INVALID_CREDENTIALS", "VERIFICATION_ERROR", "LOCKED_OUT")
# How the first four staff share the Microsoft 365 admin actions, so one actor stands out
ADMIN_WEIGHTS = (8, 4, 2, 1)

# What each Beats source's envelope carries
DATASETS = {
    "okta": {"dataset": "okta.system", "input": "httpjson"},
    "crowdstrike": {"dataset": "crowdstrike.falcon", "input": "filestream"},
    "o365": {"dataset": "o365.audit", "input": "o365audit"},
}
OKTA_TIME = "%Y-%m-%dT%H:%M:%S.%f"
O365_TIME = "%Y-%m-%dT%H:%M:%S"
# The Falcon stream sends epoch times: milliseconds for these, seconds for the process times
FALCON_MS_FIELDS = ("UTCTimestamp",)
FALCON_SECONDS_FIELDS = ("ProcessStartTime", "ProcessEndTime")
REPLAY_TIMEOUT_SECONDS = 30
REPLAY_BATCH = 50


def _firewall_module() -> ModuleType:
    """The demo's firewall generator, whose ASA and FortiGate line builders the cyber pack reuses."""
    spec = importlib.util.spec_from_file_location(
        name="demo_firewall", location=FIREWALL_GENERATOR
    )
    if (spec is None) or (spec.loader is None):
        sys.exit(f"cannot load {FIREWALL_GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    # No __pycache__ beside the demo's own generator, which may be read-only
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    return module


def _firewall_lines(*, rng: random.Random) -> dict[str, list[str]]:
    """The ASA and FortiGate sets with the suspects mixed into the outside addresses."""
    firewall = _firewall_module()
    # The line builders pick outside addresses from this module global at call time
    firewall.OUTSIDE = [*firewall.OUTSIDE, *(SUSPECTS * SUSPECT_WEIGHT)]
    return {
        "cisco-asa": firewall._asa_lines(rng=rng),
        "fortinet": firewall._fortinet_lines(rng=rng),
    }


def _read_records(*, path: Path) -> list[dict]:
    """The JSON object on each non-blank line of one sample file."""
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _weighted(rng: random.Random, table: Sequence[tuple]) -> tuple:
    """One row of a table whose last column is its weight."""
    return rng.choices(table, weights=[row[-1] for row in table])[0]


def _geo(place: tuple) -> dict:
    """An Okta geographicalContext for one COUNTRIES row."""
    city, state, country, lat, lon, _ = place
    return {
        "city": city,
        "country": country,
        "geolocation": {"lat": lat, "lon": lon},
        "state": state,
    }


def _user(rng: random.Random, *, users: Sequence[str]) -> tuple[str, str]:
    """A staff login and display name."""
    name = rng.choice(users)
    return f"{name}@{DOMAIN}", name.capitalize()


def _scrubbed(value: object) -> object:
    """A copy of one sample value with every upstream domain and tenant name moved onto the example domains."""
    if isinstance(value, dict):
        return {key: _scrubbed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrubbed(item) for item in value]
    if not (isinstance(value, str)):
        return value
    for pattern, replacement in DOMAIN_MAP:
        value = pattern.sub(replacement, value)
    return TENANT.sub(
        lambda m: "Example" if m.group(0)[0].isupper() else "example", value
    )


def _user_key(login: str) -> str:
    """One staff member's stable 16-hex-digit Microsoft 365 user key."""
    return hashlib.sha256(login.encode("utf-8")).hexdigest()[:16].upper()


def _okta_records(
    *, rng: random.Random, samples: Sequence[dict], users: Sequence[str]
) -> list[dict]:
    """Okta system log events, mostly sign-ins, the suspects failing from abroad."""
    templates: dict[str, list[dict]] = {}
    for record in samples:
        if ("eventType" in record) and ("published" in record):
            templates.setdefault(record["eventType"], []).append(record)
    starts = templates.pop("user.session.start")
    others = sorted(templates)
    out = []
    stamp = EPOCH
    for _ in range(LINES):
        stamp += timedelta(milliseconds=rng.randint(20, 180))
        suspect = False
        if rng.random() < 0.72:
            record = copy.deepcopy(rng.choice(starts))
            suspect = rng.random() < 0.12
            failed = suspect or rng.random() < 0.15
            outcome = {"result": "FAILURE" if failed else "SUCCESS"}
            if failed:
                outcome["reason"] = rng.choice(OKTA_FAILURES)
            record["outcome"] = outcome
        else:
            record = copy.deepcopy(rng.choice(templates[rng.choice(others)]))
        login, display = _user(rng, users=users)
        address = rng.choice(SUSPECTS if suspect else STAFF_ADDRESSES)
        # A suspect always signs in from the same place
        place = (
            SUSPECT_COUNTRIES[SUSPECTS.index(address) % len(SUSPECT_COUNTRIES)]
            if suspect
            else _weighted(rng, COUNTRIES)
        )
        record["actor"] = {
            **record.get("actor", {}),
            "alternateId": login,
            "displayName": display,
            "id": f"00u{rng.getrandbits(64):016x}",
            "type": "User",
        }
        client = record.setdefault("client", {})
        client["ipAddress"] = address
        client["geographicalContext"] = _geo(place)
        record["request"] = {
            "ipChain": [
                {"geographicalContext": _geo(place), "ip": address, "version": "V4"}
            ]
        }
        if "securityContext" in record:
            record["securityContext"] = dict(
                SUSPECT_NETWORK if suspect else STAFF_NETWORK
            )
        record["uuid"] = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        record["published"] = stamp.strftime(OKTA_TIME)[:-3] + "Z"
        out.append(record)
    return out


def _epoch_ms(when: datetime) -> int:
    """Epoch milliseconds, as the Falcon stream writes `eventCreationTime`."""
    return int(when.timestamp() * 1000)


def _falcon_times(*, event: dict, when: datetime) -> None:
    """Set the Falcon event's own times that it carries to `when`, in the stream's epoch units."""
    for key in FALCON_MS_FIELDS:
        if key in event:
            event[key] = _epoch_ms(when)
    for key in FALCON_SECONDS_FIELDS:
        if key in event:
            event[key] = int(when.timestamp())


def _falcon_records(
    *, rng: random.Random, samples: Sequence[dict], users: Sequence[str]
) -> list[dict]:
    """CrowdStrike Falcon streaming events: endpoint detections and console audit."""
    templates: dict[str, list[dict]] = {}
    for record in samples:
        kind = (record.get("metadata") or {}).get("eventType")
        if kind and set(record) == {"event", "metadata"}:
            templates.setdefault(kind, []).append(record)
    detections = templates["EppDetectionSummaryEvent"]
    audits = templates["AuthActivityAuditEvent"]
    out = []
    stamp = EPOCH
    offset = rng.randint(100_000, 900_000)
    for _ in range(LINES):
        stamp += timedelta(milliseconds=rng.randint(20, 180))
        offset += 1
        login, _ = _user(rng, users=users)
        if rng.random() < 0.6:
            record = copy.deepcopy(rng.choice(detections))
            name, tactic, technique, technique_id, objective = rng.choice(DETECTIONS)
            severity, score, _ = _weighted(rng, SEVERITIES)
            host = rng.choice(HOSTS)
            composite = f"{rng.getrandbits(64):016x}:ind:{rng.getrandbits(64):016x}"
            record["event"].update(
                {
                    "CompositeId": composite,
                    "ComputerName": host,
                    "Hostname": host,
                    "LocalIP": f"10.10.{rng.randint(10, 50)}.{rng.randint(5, 250)}",
                    "Name": name,
                    "Objective": objective,
                    "Severity": score,
                    "SeverityName": severity,
                    "Tactic": tactic,
                    "Technique": technique,
                    "TechniqueId": technique_id,
                    "UserName": login.split("@")[0],
                }
            )
        else:
            record = copy.deepcopy(rng.choice(audits))
            suspect = rng.random() < 0.1
            record["event"].update(
                {
                    "Success": not (suspect) and rng.random() > 0.08,
                    "UserId": login,
                    "UserIp": rng.choice(SUSPECTS if suspect else STAFF_ADDRESSES),
                }
            )
            # The console's audit names its actor and target again among its key-value pairs
            target = f"{rng.choice(users)}@{DOMAIN}"
            for pair in record["event"].get("AuditKeyValues") or []:
                if pair.get("Key") == "actor_user":
                    pair["ValueString"] = login
                elif pair.get("Key") == "target_name":
                    pair["ValueString"] = target
        _falcon_times(event=record["event"], when=stamp)
        record["metadata"].update(
            {"eventCreationTime": _epoch_ms(stamp), "offset": offset}
        )
        out.append(record)
    return out


def _o365_records(
    *, rng: random.Random, samples: Sequence[dict], users: Sequence[str]
) -> list[dict]:
    """Microsoft 365 unified audit records, weighted towards sign-ins and admin actions."""
    groups: dict[str, list[dict]] = {"admin": [], "login": [], "file": [], "other": []}
    for wrapper in samples:
        record = wrapper["o365audit"]
        operation = str(record.get("Operation", ""))
        if (record.get("RecordType") == 1) or ("Admin" in operation):
            groups["admin"].append(record)
        elif operation == "UserLoggedIn":
            groups["login"].append(record)
        elif operation.startswith("File"):
            groups["file"].append(record)
        else:
            groups["other"].append(record)
    mix = (("admin", 22), ("login", 28), ("file", 40), ("other", 10))
    admins = users[:4]
    out = []
    stamp = EPOCH
    for _ in range(LINES):
        stamp += timedelta(milliseconds=rng.randint(20, 180))
        group = _weighted(rng, mix)[0]
        record = copy.deepcopy(rng.choice(groups[group]))
        if group == "admin":
            name = rng.choices(admins, weights=ADMIN_WEIGHTS)[0]
        else:
            name = rng.choice(users)
        login = f"{name}@{DOMAIN}"
        address = rng.choice(STAFF_ADDRESSES)
        record.update(
            {
                "ClientIP": address,
                "CreationTime": stamp.strftime(O365_TIME),
                "Id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                "UserId": login,
            }
        )
        if "ActorIpAddress" in record:
            record["ActorIpAddress"] = address
        # One record, one identity: the actor and user key name the same person as UserId
        key = _user_key(login)
        for actor in record.get("Actor") or []:
            if "@" in str(actor.get("ID", "")):
                actor["ID"] = login
            elif actor.get("Type") == 3:
                actor["ID"] = key
        held = str(record.get("UserKey", ""))
        record["UserKey"] = (
            f"{held.rpartition('|')[0]}|{key.lower()}@{DOMAIN}"
            if "|membership|" in held
            else f"{key}@{DOMAIN}"
        )
        if (group == "login") and (rng.random() < 0.1):
            record["ResultStatus"] = "Failed"
        out.append({"o365audit": record})
    return out


def envelope(*, record: dict, source: str) -> dict:
    """Wrap one record the way an Elastic Agent ships it, routed on `data_stream.dataset`.

    Args:
        record: The vendor's own record. o365's is the object under `o365audit`, the others a JSON object sent as `message`.
        source: The cyber source the record belongs to, a key of DATASETS.

    Returns:
        The body dfe-receiver is posted.
    """
    meta = DATASETS[source]
    body: dict = {
        "@timestamp": EPOCH.strftime(OKTA_TIME)[:-3] + "Z",
        "agent": {"name": "edge-agent-01", "type": "filebeat"},
        "data_stream": {
            "dataset": meta["dataset"],
            "namespace": "default",
            "type": "logs",
        },
        "input": {"type": meta["input"]},
    }
    if "o365audit" in record:
        body["o365audit"] = record["o365audit"]
    else:
        body["message"] = json.dumps(record, separators=(",", ":"))
    return body


def write_all(*, fixtures: Path) -> dict[str, int]:
    """Write every cyber source's demo.log and return the lines each holds."""
    rng = random.Random(SEED)
    users = list(_firewall_module().USERS)
    lines = _firewall_lines(rng=rng)
    builders: Mapping[str, tuple[Path, Callable[..., list[dict]]]] = {
        "okta": (fixtures / "okta" / "panther-okta-systemlog.ndjson", _okta_records),
        "crowdstrike": (
            fixtures / "crowdstrike" / "panther-crowdstrike.ndjson",
            _falcon_records,
        ),
        "o365": (fixtures / "o365" / "botsv3-o365audit.log", _o365_records),
    }
    for source, (path, build) in builders.items():
        records = [
            _scrubbed(record)
            for record in build(rng=rng, samples=_read_records(path=path), users=users)
        ]
        lines[source] = [
            json.dumps(envelope(record=record, source=source), separators=(",", ":"))
            for record in records
        ]
    written = {}
    for source, body in lines.items():
        target = HERE / source / "demo.log"
        target.parent.mkdir(exist_ok=True)
        target.write_text(data="\n".join(body) + "\n", encoding="utf-8", newline="\n")
        written[source] = len(body)
    return written


def stamped(*, body: dict, now: datetime) -> dict:
    """Copy one envelope with `@timestamp` and its record's own time set to `now`.

    Args:
        body: One line of a Beats source's demo.log, parsed.
        now: The send time.

    Returns:
        The envelope to post.
    """
    out = copy.deepcopy(body)
    out["@timestamp"] = now.strftime(OKTA_TIME)[:-3] + "Z"
    if "o365audit" in out:
        out["o365audit"]["CreationTime"] = now.strftime(O365_TIME)
        return out
    record = json.loads(out["message"])
    if "published" in record:
        record["published"] = out["@timestamp"]
    else:
        record["metadata"]["eventCreationTime"] = _epoch_ms(now)
        _falcon_times(event=record["event"], when=now)
    out["message"] = json.dumps(record, separators=(",", ":"))
    return out


def _post(*, bodies: list[dict], target: str) -> None:
    """POST one batch as a JSON array, which dfe-receiver splits per element."""
    request = urllib.request.Request(
        data=json.dumps(bodies).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
        url=target,
    )
    with urllib.request.urlopen(request, timeout=REPLAY_TIMEOUT_SECONDS) as reply:
        if reply.status not in (200, 202, 204):
            sys.exit(f"{target} answered HTTP {reply.status}")


def replay(
    *, duration: float, loop: bool, rate: float, sources: Sequence[str], target: str
) -> int:
    """Send each source's lines at `rate` lines a second per source and return how many went."""
    streams = []
    for source in sources:
        if source not in DATASETS:
            sys.exit(f"{source!r} is not a Beats source. Pick from {sorted(DATASETS)}")
        text = (HERE / source / "demo.log").read_text(encoding="utf-8")
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        streams.append(itertools.cycle(records) if loop else iter(records))
    sent = 0
    deadline = time.monotonic() + duration
    interval = REPLAY_BATCH / rate
    while time.monotonic() < deadline:
        started = time.monotonic()
        now = datetime.now(UTC)
        batch = []
        for stream in streams:
            batch.extend(
                stamped(body=body, now=now)
                for body in itertools.islice(stream, REPLAY_BATCH)
            )
        if not (batch):
            break
        try:
            _post(bodies=batch, target=target)
        except OSError as error:
            # A receiver restart drops the connection, and the next batch tries again
            print(f"replay: {target}: {error}", file=sys.stderr, flush=True)
        else:
            sent += len(batch)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))
    return sent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        help="dfe-transform-elastic's tests/fixtures/unencumbered, the samples the Beats sources are built from",
    )
    commands = parser.add_subparsers(dest="command")
    send = commands.add_parser(
        "replay", help="post the Beats sources' lines at dfe-receiver's HTTP ingest"
    )
    send.add_argument("--target", required=True, help="the receiver's /ingest URL")
    send.add_argument(
        "--sources", default=",".join(DATASETS), help="comma-separated Beats sources"
    )
    send.add_argument(
        "--rate", type=float, default=10.0, help="lines a second per source"
    )
    send.add_argument("--loop", action="store_true", help="start again at the end")
    send.add_argument(
        "--duration", type=float, default=600.0, help="stop after this many seconds"
    )
    args = parser.parse_args(argv)
    if args.command == "replay":
        if args.rate <= 0:
            parser.error("--rate must be above 0")
        sent = replay(
            duration=args.duration,
            loop=args.loop,
            rate=args.rate,
            sources=[name for name in args.sources.split(",") if name],
            target=args.target,
        )
        print(f"replay: sent {sent} lines", flush=True)
        return 0
    if (args.fixtures is None) or not (args.fixtures.is_dir()):
        parser.error("--fixtures names no directory of upstream samples")
    for source, count in write_all(fixtures=args.fixtures).items():
        print(f"{source}/demo.log: {count} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
