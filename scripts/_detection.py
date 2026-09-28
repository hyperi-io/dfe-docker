#  Project:      dfe-docker
#  File:         _detection.py
#  Purpose:      Rule, hunt and verdict primitives for the e2e rules-and-hunts test
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Primitives for the e2e test that proves a rule and a hunt detect what they should.

Internal support module - imported by the e2e suite, not executed directly. Stdlib
only, so the unit tests import it without PyYAML.

The test sends two sets of events under one run marker: events the rule must match
and events it must not. Each carries a label in `_tags`, so a landed row says which
event it is, and the detection table names the row it matched by `matched_uuid`.
The verdict joins the two: the hunt's detections have to be exactly the landed
rows of the matching set, each once, under the rule and severity it was given.
Talking to the engine and polling stay with the suite, which owns the stack.
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


def event_label(kind: str, index: int) -> str:
    """Name one event, so a landed row says which set it came from."""
    return f"{kind}-{index:03d}"


def labelled_events(
    *, marker: str, test_name: str, source: str, events: Mapping[str, Sequence[dict]]
) -> list[tuple[str, str]]:
    """Return (label, request body) for every event, each stamped with the run marker.

    Args:
        marker: The run's marker, shared by every event.
        test_name: The e2e test the events belong to.
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


def rule_sql(*, database: str, table: str, marker: str, where: str) -> str:
    """The rule's SELECT: the detection logic, held to this run's rows by its marker."""
    held = f"{MARKER_EXPRESSIONS[0]} = '{escape_literal(marker)}'"
    return f"SELECT * FROM {database}.{table} WHERE {held} AND ({where})"


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


def landed_labels(tsv: str) -> dict[str, list[str]]:
    """Map each label to its rows' uuids, read from `uuid<TAB>label` lines."""
    landed: dict[str, list[str]] = {}
    for line in tsv.splitlines():
        uuid, _, label = line.partition("\t")
        if uuid and label:
            landed.setdefault(label, []).append(uuid)
    return landed


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
