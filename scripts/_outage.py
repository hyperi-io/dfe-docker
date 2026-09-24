#  Project:      dfe-docker
#  File:         _outage.py
#  Purpose:      Load, answers and container state for the backing-service outage tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Primitives for the e2e tests that stop a backing service under load.

Internal support module - imported by the e2e suite, not executed directly. Stdlib
only, so the unit tests import it without PyYAML.

A stack survives an outage when no DFE container exits or restarts, every request
the receiver takes gets an HTTP answer, every record it accepted lands once the
service is back, and every Kafka consumer catches up again. This module holds the
load that makes those claims testable and the readers that judge them. Starting
and stopping containers stays with the suite, which owns the compose project.
"""

import http.client
import json
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _common import _config_topics
from _pipeline import MARKER_EXPRESSIONS, ch_int, ch_query, escape_literal, marked_event

PHASES = ("before", "during", "after")


def record_marker(prefix: str, seq: int) -> str:
    """Name one record, so a landed row says which request carried it."""
    return f"{prefix}-{seq:06d}"


def seq_of(marker: str, prefix: str) -> int | None:
    """Return the sequence number in a record marker, or None when it is not this load's."""
    head = f"{prefix}-"
    if not marker.startswith(head):
        return None
    tail = marker[len(head) :]
    return int(tail) if tail.isdigit() else None


@dataclass(frozen=True, slots=True)
class Sent:
    """One request the load made, and what came back.

    Attributes:
        seq: The record's sequence number, carried in its marker.
        source: The `_source` the record was routed by.
        phase: Which part of the outage the request was sent in.
        status: The HTTP status, or None when no answer arrived.
        error: Why no answer arrived, empty when one did.
        seconds: How long the request took.
    """

    seq: int
    source: str
    phase: str
    status: int | None
    error: str
    seconds: float

    @property
    def accepted(self) -> bool:
        """Whether the receiver took the record, which obliges it to land."""
        return self.status is not None and 200 <= self.status < 300

    @property
    def outcome(self) -> str:
        """The answer as one label: the status, or what stood in for one."""
        return str(self.status) if self.status is not None else f"none ({self.error})"


def post_record(url: str, body: str, timeout: float) -> tuple[int | None, str]:
    """POST one record and return (status, error), with status None when nothing answered."""
    request = Request(url, data=body.encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, ""
    except HTTPError as error:
        error.close()
        return error.code, ""
    except (URLError, OSError, http.client.HTTPException) as error:
        reason = getattr(error, "reason", error)
        return None, reason if isinstance(reason, str) else type(reason).__name__


def interleave(events: Mapping[str, Sequence[dict]]) -> list[tuple[str, dict]]:
    """Alternate the sources record by record, so every phase reaches each one.

    Args:
        events: The records to send, keyed by the `_source` each is routed by.

    Returns:
        (source, record) pairs, one source after the next, until the longest runs out.

    Raises:
        ValueError: If no source has a record to send.
    """
    sources = [(source, rows) for source, rows in events.items() if rows]
    if not sources:
        raise ValueError("the load has no record to send")
    longest = max(len(rows) for _, rows in sources)
    records = []
    for index in range(longest):
        records.extend((source, rows[index % len(rows)]) for source, rows in sources)
    return records


class SteadyLoad:
    """Send marked records from several workers until stopped, keeping every answer.

    `phase` is read as each request goes out, so the caller moves the whole load
    from one part of the outage to the next by assigning it.
    """

    def __init__(
        self,
        *,
        url: str,
        prefix: str,
        records: Sequence[tuple[str, dict]],
        workers: int,
        interval: float,
        timeout: float,
    ) -> None:
        """Prepare the workers without sending anything.

        Raises:
            ValueError: If there is nothing to send or nobody to send it.
        """
        if not records:
            raise ValueError("the load has no record to send")
        if workers < 1:
            raise ValueError(f"the load needs at least one worker, got {workers}")
        self.phase = PHASES[0]
        self._url = url
        self._prefix = prefix
        self._records = list(records)
        self._interval = interval
        self._timeout = timeout
        self._lock = threading.Lock()
        self._next = 0
        self._sent: list[Sent] = []
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._run, name=f"outage-load-{n}", daemon=True)
            for n in range(workers)
        ]

    def start(self) -> None:
        """Start every worker."""
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Stop sending and wait for each request in flight to get its answer."""
        self._stop.set()
        for thread in self._threads:
            thread.join(self._timeout + 5)

    def sent(self) -> list[Sent]:
        """Every request made so far."""
        with self._lock:
            return list(self._sent)

    def _claim(self) -> tuple[int, str, dict]:
        with self._lock:
            seq = self._next
            self._next += 1
        source, event = self._records[seq % len(self._records)]
        return seq, source, event

    def _run(self) -> None:
        while not self._stop.is_set():
            seq, source, event = self._claim()
            phase = self.phase
            body = marked_event(
                marker=record_marker(self._prefix, seq), source=source, extra=event
            )
            started = time.monotonic()
            status, error = post_record(self._url, body, self._timeout)
            record = Sent(
                seq=seq,
                source=source,
                phase=phase,
                status=status,
                error=error,
                seconds=time.monotonic() - started,
            )
            with self._lock:
                self._sent.append(record)
            self._stop.wait(self._interval)


def outcomes_by_phase(sent: Iterable[Sent]) -> dict[str, Counter[str]]:
    """Count each answer the receiver gave, per phase."""
    table: dict[str, Counter[str]] = {phase: Counter() for phase in PHASES}
    for record in sent:
        table.setdefault(record.phase, Counter())[record.outcome] += 1
    return table


def slowest_by_phase(sent: Iterable[Sent]) -> dict[str, float]:
    """The longest any one request took, per phase."""
    slowest: dict[str, float] = {}
    for record in sent:
        slowest[record.phase] = max(slowest.get(record.phase, 0.0), record.seconds)
    return slowest


def accepted_by_seq(sent: Iterable[Sent], source: str) -> dict[int, str]:
    """Map each record of `source` the receiver accepted to the phase it was sent in."""
    return {r.seq: r.phase for r in sent if r.accepted and r.source == source}


def landed_seqs(
    database: str,
    table: str,
    prefix: str,
    *,
    debug: Callable[[str], None] | None = None,
) -> set[int] | None:
    """Every record of this load that has landed, or None when no marker can be read.

    An expression that runs and matches nothing is only trusted once it reads some
    row's marker, because a fallback that does not fit the schema returns '' for
    every row and would report loss that never happened.
    """
    head = escape_literal(f"{prefix}-")
    readable = False
    for expression in MARKER_EXPRESSIONS:
        where = f"startsWith({expression}, '{head}')"
        count = ch_int(
            f"SELECT count() FROM {database}.{table} WHERE {where}", debug=debug
        )
        if count is None:
            continue
        if count:
            raw = ch_query(
                f"SELECT DISTINCT {expression} FROM {database}.{table} "
                f"WHERE {where} FORMAT TabSeparated",
                debug=debug,
            )
            return {
                seq
                for line in raw.splitlines()
                if (seq := seq_of(line.strip(), prefix)) is not None
            }
        probe = ch_int(
            f"SELECT count() FROM {database}.{table} WHERE {expression} != ''",
            debug=debug,
        )
        readable = readable or bool(probe)
    return set() if readable else None


@dataclass(frozen=True, slots=True)
class ContainerState:
    """What `docker inspect` says about one container, reduced to what an exit changes.

    Attributes:
        container_id: The container's id.
        status: The container's state, e.g. `running`.
        health: The healthcheck's verdict, or empty when there is none.
        restarts: How many times the engine has restarted it.
        started_at: When its current process started.
        pid: Its current process id.
    """

    container_id: str
    status: str
    health: str
    restarts: int
    started_at: str
    pid: int


def container_state(inspected: Mapping) -> ContainerState:
    """Reduce one `docker inspect` document to a ContainerState."""
    state = inspected.get("State") or {}
    health = state.get("Health") or {}
    return ContainerState(
        container_id=str(inspected.get("Id", "")),
        status=str(state.get("Status", "")),
        health=str(health.get("Status", "")),
        restarts=int(inspected.get("RestartCount", 0) or 0),
        started_at=str(state.get("StartedAt", "")),
        pid=int(state.get("Pid", 0) or 0),
    )


def state_changes(before: ContainerState, after: ContainerState) -> list[str]:
    """Every sign that the container's process did not run straight through."""
    changes = []
    if after.container_id != before.container_id:
        changes.append(
            f"container replaced ({before.container_id[:12]} -> {after.container_id[:12]})"
        )
    if after.restarts != before.restarts:
        changes.append(f"restart count {before.restarts} -> {after.restarts}")
    if after.started_at != before.started_at:
        changes.append(f"process started again at {after.started_at}")
    if after.pid != before.pid:
        changes.append(f"pid {before.pid} -> {after.pid}")
    if after.status != "running":
        changes.append(f"status is {after.status or 'unknown'}")
    return changes


def exits(events_json: str) -> list[tuple[str, int, str]]:
    """Return (container id, unix time, what happened) per die or OOM event in `docker events` JSON lines."""
    found = []
    for line in events_json.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        actor = event.get("Actor") or {}
        attributes = actor.get("Attributes") or {}
        action = str(event.get("Action") or event.get("status") or "")
        code = attributes.get("exitCode")
        label = f"{action} with exit code {code}" if code is not None else action
        container = str(actor.get("ID") or event.get("id") or "")
        found.append((container, int(event.get("time", 0) or 0), label))
    return found


def failure_lines(log: str, limit: int) -> list[str]:
    """The last `limit` lines of a log that report an error, a fatal exit or a panic."""
    markers = ("ERROR", "fatal", "panic")
    hits = [line for line in log.splitlines() if any(m in line for m in markers)]
    return hits[-limit:]


def expected_consumers(configs: Iterable[Path]) -> Counter[str]:
    """Count the services each topic is named as a subscription of, across the configs."""
    counts: Counter[str] = Counter()
    for path in configs:
        subscribed, _, _ = _config_topics(path=path)
        counts.update(subscribed)
    return counts


def group_lag_json(described: str) -> dict[str, dict[str, int]]:
    """Map each consumer group to its lag per topic, from `rpk group describe --format json`.

    A partition the group has never committed on is left out, so a group appears
    under a topic only once it has consumed from it.

    Raises:
        ValueError: If the text is not the JSON rpk prints.
    """
    groups = json.loads(described or "[]") or []
    lag: dict[str, dict[str, int]] = {}
    for group in groups:
        per_topic: dict[str, int] = {}
        for partition in group.get("partitions") or []:
            if int(partition.get("current_offset", -1)) < 0:
                continue
            topic = str(partition.get("topic", ""))
            per_topic[topic] = per_topic.get(topic, 0) + int(partition.get("lag") or 0)
        lag[str(group.get("group_name", ""))] = per_topic
    return lag


def group_lag_table(described: str) -> dict[str, dict[str, int]]:
    """Map each consumer group to its lag per topic, from `kafka-consumer-groups.sh --describe`.

    Same rule as the rpk reader: a partition with no committed offset (`-`) is left out.
    """
    lag: dict[str, dict[str, int]] = {}
    columns: list[str] = []
    for line in described.splitlines():
        cells = line.split()
        if cells[:2] == ["GROUP", "TOPIC"]:
            columns = cells
            continue
        if not columns or len(cells) < len(columns):
            continue
        row = dict(zip(columns, cells, strict=False))
        if not row.get("CURRENT-OFFSET", "-").isdigit():
            continue
        per_topic = lag.setdefault(row["GROUP"], {})
        behind = int(row["LAG"]) if row.get("LAG", "-").isdigit() else 0
        per_topic[row["TOPIC"]] = per_topic.get(row["TOPIC"], 0) + behind
    return lag


def consumers_behind(
    lag: Mapping[str, Mapping[str, int]],
    expected: Mapping[str, int],
    watched: Iterable[str] = (),
) -> list[str]:
    """Say what keeps the consumers of the stack's topics from having caught up.

    Each expected topic needs at least as many committing groups as services
    subscribe to it. Every group committing on an expected or watched topic needs
    zero lag there, which reaches a consumer that subscribes by pattern. Empty
    means caught up.
    """
    problems = []
    for topic, wanted in sorted(expected.items()):
        groups = sorted(group for group, topics in lag.items() if topic in topics)
        if len(groups) < wanted:
            problems.append(
                f"'{topic}': {len(groups)} of {wanted} consumer group(s) have committed "
                f"({', '.join(groups) or 'none'})"
            )
    for topic in sorted(set(expected) | set(watched)):
        for group in sorted(lag):
            behind = lag[group].get(topic, 0)
            if behind:
                problems.append(f"'{topic}': group '{group}' is {behind} behind")
    return problems
