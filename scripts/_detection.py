#  Project:      dfe-docker
#  File:         _detection.py
#  Purpose:      Rule, hunt and verdict primitives for the e2e rules-and-hunts test and POST
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Primitives for proving a rule and a hunt detect what they should.

Internal support module - imported by the e2e suite and the power-on self test, not
executed directly. Stdlib only, so the unit tests import it without PyYAML.

A caller sends two sets of events under one run marker: events the rule must match
and events it must not. Each carries a label in `_tags`, so a landed row says which
event it is, and the detection table names the row it matched by `matched_uuid`.
The verdict joins the two: the hunt's detections have to be exactly the landed
rows of the matching set, each once, under the rule and severity it was given.
Talking to the engine, polling and reporting stay with the callers.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from _pipeline import MARKER_EXPRESSIONS, escape_literal, marked_event

MATCHING = "matching"
OTHER = "other"
# The tightest schedule a hunt config expresses, which a queued run shortens.
HUNT_CRON = "* * * * *"
SEVERITY = "high"
# Each event's label rides in `_tags` beside the marker, which the landing schema keeps.
LABEL_EXPRESSION = "toString(`_tags`.event)"

POLL_SECONDS = 3.0
# The runner beats at the top of every 15s poll, from its first one.
RUNNER_ALIVE_TIMEOUT_SECONDS = 120.0
# The runner re-reads hunts each poll, and a hunt with no watermark is due at once.
PICKUP_TIMEOUT_SECONDS = 120.0
# A queued run waits one poll, and the next scheduled fire a minute on is the backstop.
COVER_TIMEOUT_SECONDS = 150.0
# A clock that disagrees with the last row's load time costs no more than this.
QUEUE_DELAY_CAP_SECONDS = 2.0


def event_label(kind: str, index: int) -> str:
    """Name one event, so a landed row says which set it came from."""
    return f"{kind}-{index:03d}"


def set_labels(events: Mapping[str, Sequence[object]], kind: str) -> list[str]:
    """Return the labels `labelled_events` gives one set's events, in order."""
    return [event_label(kind, index) for index in range(len(events[kind]))]


def labelled_events(
    *, marker: str, test_name: str, source: str, events: Mapping[str, Sequence[dict]]
) -> list[tuple[str, str]]:
    """Return (label, request body) for every event, each stamped with the run marker.

    Args:
        marker: The run's marker, shared by every event.
        test_name: The test the events belong to.
        source: The `_source` the receiver routes them by.
        events: The events of each set, keyed MATCHING or OTHER.

    Returns:
        One pair per event, in set order.
    """
    bodies = []
    for kind in sorted(events):
        for index, event in enumerate(events[kind]):
            label = event_label(kind, index)
            tagged = {**event, "_tags": {"test_name": test_name, "event": label}}
            bodies.append(
                (label, marked_event(marker=marker, source=source, extra=tagged))
            )
    return bodies


def _held(marker: str) -> str:
    """The predicate that holds a query to one run's rows."""
    return f"{MARKER_EXPRESSIONS[0]} = '{escape_literal(marker)}'"


def rule_sql(*, database: str, table: str, marker: str, where: str) -> str:
    """The rule's SELECT: the detection logic, held to this run's rows by its marker."""
    return f"SELECT * FROM {database}.{table} WHERE {_held(marker)} AND ({where})"


def rule_request(
    *, name: str, database: str, table: str, marker: str, where: str
) -> dict:
    """The POST /rules body for a raw rule over `database.table`, held to one run."""
    return {
        "name": name,
        "severity": SEVERITY,
        "source_type": "raw",
        "user_sql": rule_sql(
            database=database, table=table, marker=marker, where=where
        ),
    }


def rule_fault(*, name: str, status: int, body: object, source: str) -> str:
    """Why POST /rules did not store a rule that runs over `source`; empty when it did.

    Args:
        name: The rule that was created.
        status: The HTTP status POST /rules answered.
        body: Its decoded body.
        source: The `database.table` the rule has to read.

    Returns:
        The fault in one line, or an empty string.
    """
    if status != 201 or not (isinstance(body, dict)):
        return f"POST /rules returned HTTP {status}: {body}"
    stored = body.get("rule") or {}
    over = f"{stored.get('source_db')}.{stored.get('source_table')}"
    if body.get("sql_errors") or over != source:
        return (
            f"the engine stored rule '{name}' over '{over}' with SQL errors "
            f"{body.get('sql_errors')}"
        )
    return ""


def hunt_request(
    *, name: str, rule: str, customer: str, source: str, target: str
) -> dict:
    """The POST /hunts body for a one-rule hunt over `source` into `target`."""
    return {
        "name": name,
        "cron": HUNT_CRON,
        "customers": [customer],
        "rules": [rule],
        "global_source_table_name": source,
        "global_target_table_name": target,
        "checkpoint_timestamp_field": "_timestamp_load",
    }


def runner_alive(*, status: int, body: object) -> bool:
    """Whether GET /hunts/status says a runner beat within its last two polls."""
    return status == 200 and isinstance(body, dict) and body.get("running") is True


def hunt_row(*, status: int, body: object, name: str) -> dict | None:
    """One hunt's row off a GET /hunts page, which carries its run state, or None."""
    if status != 200 or not (isinstance(body, dict)):
        return None
    for row in body.get("items") or []:
        if isinstance(row, dict) and row.get("name") == name:
            return row
    return None


def queue_delay(*, last_load_ms: int, now: float) -> float:
    """Seconds to wait before queueing, so the queued fire lands past the last row.

    The engine stamps a queued fire in whole seconds and the window it runs ends
    before that second, so the fire has to be a whole second on from the last row.
    """
    wait = last_load_ms / 1000 + 1.0 - now
    return max(0.0, min(QUEUE_DELAY_CAP_SECONDS, wait))


def queued_fire(*, status: int, body: object) -> int | None:
    """The fire POST /hunts/{name}/run queued, or None unless it answered 202 queued."""
    if status != 202 or not (isinstance(body, dict)) or body.get("queued") is not True:
        return None
    try:
        return int(body.get("requested_fire", 0))
    except (TypeError, ValueError):
        return None


def covers(row: Mapping | None, last_load_ms: int) -> bool:
    """Whether the hunt's committed windows reach past the last row's load time."""
    last_run = (row or {}).get("last_run")
    return last_run is not None and last_run * 1000 > last_load_ms


def set_by(row: Mapping, queued: int | None) -> str:
    """Which fire set the watermark, as a committed run leaves its own fire there.

    It names the LAST window, not the one that scanned the rows: a scheduled fire
    between the first window and the queued one can scan them and leave the queued
    run an empty window.
    """
    if queued is not None and row.get("last_run") == queued:
        return "the queued run"
    return "a scheduled fire"


def run_state(row: Mapping | None) -> str:
    """The run state a hunts list row reports, in one line."""
    if row is None:
        return "hunts list did not answer"
    return f"last_run={row.get('last_run')} run_requested={row.get('run_requested')}"


def landed_sql(*, database: str, table: str, marker: str) -> str:
    """The read `landed_labels` parses: each of the run's rows as `uuid<TAB>label`."""
    return (
        f"SELECT toString(_uuid), {LABEL_EXPRESSION} FROM {database}.{table} "
        f"WHERE {_held(marker)} FORMAT TabSeparated"
    )


def landed_labels(tsv: str) -> dict[str, list[str]]:
    """Map each label to its rows' uuids, read from `uuid<TAB>label` lines."""
    landed: dict[str, list[str]] = {}
    for line in tsv.splitlines():
        uuid, _, label = line.partition("\t")
        if uuid and label:
            landed.setdefault(label, []).append(uuid)
    return landed


def last_load_sql(*, database: str, table: str, marker: str) -> str:
    """The read `last_load_ms` parses: when the run's last row was loaded."""
    return (
        f"SELECT toUnixTimestamp64Milli(max(_timestamp_load)) "
        f"FROM {database}.{table} WHERE {_held(marker)}"
    )


def last_load_ms(raw: str) -> int | None:
    """The last row's load time in epoch milliseconds, or None if it was unreadable."""
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _hunt_rows(*, target: str, hunt: str) -> str:
    """The FROM and WHERE that select one hunt's detections in `target`."""
    return f"FROM {target} WHERE hunt_name = '{escape_literal(hunt)}'"


def detection_count_sql(*, target: str, hunt: str) -> str:
    """Count one hunt's detections, a query that fails where `target` is absent."""
    return f"SELECT count() {_hunt_rows(target=target, hunt=hunt)}"


def detections_sql(*, target: str, hunt: str) -> str:
    """The read `detections` parses: each of one hunt's detections in three columns."""
    return (
        f"SELECT toString(matched_uuid), rule_id, severity "
        f"{_hunt_rows(target=target, hunt=hunt)} FORMAT TabSeparated"
    )


@dataclass(frozen=True, slots=True)
class Detection:
    """One row the hunt wrote to the detection table.

    Attributes:
        matched_uuid: The `_uuid` of the source row it matched.
        rule_id: The rule that matched it.
        severity: The severity it was written with.
    """

    matched_uuid: str
    rule_id: str
    severity: str


def detections(tsv: str) -> list[Detection]:
    """Parse `matched_uuid<TAB>rule_id<TAB>severity` lines into detections."""
    found = []
    for line in tsv.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            found.append(Detection(*parts))
    return found


def verdict(
    *,
    landed: Mapping[str, Sequence[str]],
    found: Iterable[Detection],
    matching: Iterable[str],
    other: Iterable[str],
    rule: str,
) -> list[str]:
    """Every way the detections differ from exactly the matching rows; empty when none.

    Args:
        landed: The uuids of this run's rows, by label.
        found: The hunt's detections.
        matching: Labels of the events the rule must match.
        other: Labels of the events it must not.
        rule: The rule id every detection should carry.

    Returns:
        One line per problem, naming the events it concerns.
    """
    matching = sorted(matching)
    other = sorted(other)
    owner = {uuid: label for label, uuids in landed.items() for uuid in uuids}
    found = list(found)
    hits = Counter(detection.matched_uuid for detection in found)

    problems = []
    unlanded = [label for label in matching + other if not landed.get(label)]
    if unlanded:
        problems.append(f"never landed, so could not be judged: {', '.join(unlanded)}")
    missed = [
        label
        for label in matching
        if landed.get(label) and not all(hits[uuid] for uuid in landed[label])
    ]
    if missed:
        problems.append(f"matching events with no detection: {', '.join(missed)}")
    wrong = sorted({owner[uuid] for uuid in hits if owner.get(uuid) in other})
    if wrong:
        problems.append(
            f"events the rule must not match were detected: {', '.join(wrong)}"
        )
    stray = sorted(uuid for uuid in hits if uuid not in owner)
    if stray:
        problems.append(f"detections of rows this run did not send: {', '.join(stray)}")
    repeated = sorted(
        owner.get(uuid, uuid) for uuid, count in hits.items() if count > 1
    )
    if repeated:
        problems.append(f"rows detected more than once: {', '.join(repeated)}")
    rules = sorted({d.rule_id for d in found if d.rule_id != rule})
    if rules:
        problems.append(f"detections under another rule id: {', '.join(rules)}")
    severities = sorted({d.severity for d in found if d.severity != SEVERITY})
    if severities:
        problems.append(
            f"detections at another severity than {SEVERITY}: {', '.join(severities)}"
        )
    return problems
