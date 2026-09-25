#  Project:      dfe-docker
#  File:         _outage.py
#  Purpose:      Load, answers and container state for the backing-service outage tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Primitives for the e2e tests that stop, kill or pause a service under load.

Internal support module - imported by the e2e suite, not executed directly. Stdlib
only, so the unit tests import it without PyYAML.

A stack survives an outage when no DFE container exits or restarts, every request
the receiver takes gets an HTTP answer, every record it accepted lands once the
service is back, and every Kafka consumer catches up again. This module holds the
load that makes those claims testable and the readers that judge them. Starting
and stopping containers stays with the suite, which owns the compose project.
"""

import base64
import binascii
import http.client
import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _common import _config_topics
from _pipeline import MARKER_EXPRESSIONS, ch_int, ch_query, escape_literal, marked_event

PHASES = ("before", "during", "after")

# `TERM` is `docker compose stop`, SIGTERM with the SIGKILL fallback, and `KILL`
# is SIGKILL at once.
SIGNALS = ("TERM", "KILL")
# What an accepted record that never lands means. `forbidden` fails on one,
# `required` fails when there is none, so a control that could not lose shows
# as non-discriminating, `unreliable` reports that instead of failing, and
# `tolerated` only records the count.
LOSS_MODES = ("forbidden", "required", "unreliable", "tolerated")
OUTAGE_KEYS = frozenset(
    {
        "service",
        "seconds",
        "signal",
        "pause",
        "workers",
        "interval",
        "loss",
        "expect_refusals",
        "spool",
        "poison",
        "dead_letters",
        "archive",
        "sources",
    }
)
PAUSE_KEYS = frozenset({"service", "seconds"})
SPOOL_KEYS = frozenset({"service", "path", "must_stay_empty", "must_replay"})
POISON_KEYS = frozenset({"data_file", "every"})
DEAD_LETTER_KEYS = frozenset({"service", "path"})
ARCHIVE_KEYS = frozenset({"services", "path"})
# The metric a pipeline counts a dead letter in when it has nowhere to put it.
DEAD_LETTERS_DROPPED = "pipeline_dead_letters_dropped_total"


@dataclass(frozen=True, slots=True)
class OutagePlan:
    """What one outage test does to the stack, and what it expects of the result.

    Attributes:
        service: The service stopped or killed, empty when the test only pauses one.
        seconds: How long that service stays down.
        workers: How many load workers send at once.
        interval: Seconds each worker waits after an answer before its next request.
        signal: How it is taken down, one of SIGNALS.
        pause_service: A service frozen first, empty for none.
        pause_seconds: How long it stays frozen before the signal.
        loss: What a lost accepted record means, one of LOSS_MODES.
        expect_refusals: Fail unless some request was refused while the outage ran.
        spool_service: The service whose disk spool is read, empty for none.
        spool_path: The spool directory inside that service's container.
        spool_must_stay_empty: Fail when anything is written under the spool.
        spool_must_replay: Fail unless every record spooled at the signal lands.
        poison_file: Records the sink refuses, sent among the good ones.
        poison_every: One record in this many is a poison record.
        dead_letter_service: The service that dead-letters what the sink refuses.
        dead_letter_path: Its file dead-letter directory.
        archive_services: The archivers whose output must hold every accepted record.
        archive_path: Their shared archive directory.
        sources: The data file the load sends per `_source`, empty for the test's own.
    """

    service: str
    seconds: int
    workers: int
    interval: float
    signal: str = "TERM"
    pause_service: str = ""
    pause_seconds: int = 0
    loss: str = "forbidden"
    expect_refusals: bool = False
    spool_service: str = ""
    spool_path: str = ""
    spool_must_stay_empty: bool = False
    spool_must_replay: bool = False
    poison_file: str = ""
    poison_every: int = 0
    dead_letter_service: str = ""
    dead_letter_path: str = ""
    archive_services: tuple[str, ...] = ()
    archive_path: str = ""
    sources: Mapping[str, str] = field(default_factory=dict)


def _whole_number(value: object, what: str) -> int:
    """Return `value` as a whole number above 0, else raise naming `what`."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{what} must be a whole number above 0, got {value!r}")
    try:
        number = int(value)
    except ValueError:
        raise ValueError(
            f"{what} must be a whole number above 0, got {value!r}"
        ) from None
    if number < 1:
        raise ValueError(f"{what} must be a whole number above 0, got {value!r}")
    return number


def _seconds_from_zero(value: object, what: str) -> float:
    """Return `value` as seconds, 0 allowed, else raise naming `what`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(
            f"{what} must be a number of seconds, 0 or more, got {value!r}"
        )
    return float(value)


def _unknown_keys(block: Mapping, known: frozenset[str], what: str) -> None:
    """Raise when `block` carries a key outside `known`, so a typo is not a silent default."""
    unknown = sorted(str(key) for key in block if key not in known)
    if unknown:
        raise ValueError(f"{what} has unknown key(s): {', '.join(unknown)}")


def _flag(block: Mapping, key: str, what: str) -> bool:
    """Return a YAML boolean, refusing a string that only looks like one."""
    value = block.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{what} '{key}' must be true or false, got {value!r}")
    return value


def _block(outage: Mapping, key: str, known: frozenset[str], needs: tuple[str, ...]):
    """Return the `key` mapping of an outage block, empty when absent.

    Raises:
        ValueError: If it is not a mapping, carries an unknown key, or misses one
            of `needs`.
    """
    block = outage.get(key) or {}
    if not isinstance(block, Mapping):
        raise ValueError(f"outage '{key}' must map {', '.join(needs)}")
    _unknown_keys(block, known, f"outage '{key}'")
    if block and not all(block.get(name) for name in needs):
        raise ValueError(f"outage '{key}' needs {' and '.join(repr(n) for n in needs)}")
    return block


def outage_plan(
    outage: Mapping,
    default_seconds: int,
    *,
    default_workers: int,
    default_interval: float,
) -> OutagePlan:
    """Validate a test's `outage:` block and return what it asks for.

    Args:
        outage: The block as the test definition carries it.
        default_seconds: How long a service stays down when `seconds` is absent.
        default_workers: How many load workers send when `workers` is absent.
        default_interval: Each worker's pause between requests when `interval`
            is absent.

    Returns:
        The plan the suite runs.

    Raises:
        ValueError: If a key is unknown, a value is out of range, or the block
            neither takes a service down nor pauses one.
    """
    _unknown_keys(outage, OUTAGE_KEYS, "outage")
    service = str(outage.get("service") or "")
    pause = outage.get("pause") or {}
    if not isinstance(pause, Mapping):
        raise ValueError("outage 'pause' must map 'service' and 'seconds'")
    _unknown_keys(pause, PAUSE_KEYS, "outage 'pause'")
    if not (service) and not (pause):
        raise ValueError("outage names no 'service' to stop and no 'pause'")
    if not (service) and ("seconds" in outage or "signal" in outage):
        raise ValueError("outage 'seconds' and 'signal' need a 'service' to take down")

    signal = str(outage.get("signal", "TERM")).upper()
    if signal not in SIGNALS:
        raise ValueError(
            f"outage 'signal' must be one of {', '.join(SIGNALS)}, got {signal!r}"
        )
    seconds = (
        _whole_number(outage.get("seconds", default_seconds), "outage 'seconds'")
        if service
        else 0
    )
    workers = _whole_number(outage.get("workers", default_workers), "outage 'workers'")
    interval = _seconds_from_zero(
        outage.get("interval", default_interval), "outage 'interval'"
    )

    pause_service = str(pause.get("service") or "")
    pause_seconds = 0
    if pause:
        if not (pause_service):
            raise ValueError("outage 'pause' names no 'service'")
        if pause_service == service:
            raise ValueError("outage 'pause' cannot freeze the service it takes down")
        pause_seconds = _whole_number(pause.get("seconds"), "outage 'pause' 'seconds'")

    loss = str(outage.get("loss", "forbidden"))
    if loss not in LOSS_MODES:
        raise ValueError(
            f"outage 'loss' must be one of {', '.join(LOSS_MODES)}, got {loss!r}"
        )

    spool = _block(outage, "spool", SPOOL_KEYS, ("service", "path"))
    poison = _block(outage, "poison", POISON_KEYS, ("data_file", "every"))
    dead_letters = _block(outage, "dead_letters", DEAD_LETTER_KEYS, ("service", "path"))
    if bool(poison) != bool(dead_letters):
        raise ValueError(
            "outage 'poison' and 'dead_letters' go together: a record the sink "
            "refuses is judged by where it was dead-lettered"
        )
    poison_every = (
        _whole_number(poison.get("every"), "outage 'poison' 'every'") if poison else 0
    )
    if poison and poison_every < 2:
        raise ValueError("outage 'poison' 'every' must leave good records between")
    archive = _block(outage, "archive", ARCHIVE_KEYS, ("services", "path"))
    archive_services = archive.get("services") or []
    if not isinstance(archive_services, list):
        raise ValueError("outage 'archive' 'services' must list the archivers")

    sources = outage.get("sources") or {}
    if not isinstance(sources, Mapping):
        raise ValueError("outage 'sources' must map each _source to a data file")

    return OutagePlan(
        service=service,
        seconds=seconds,
        workers=workers,
        interval=interval,
        signal=signal,
        pause_service=pause_service,
        pause_seconds=pause_seconds,
        loss=loss,
        expect_refusals=_flag(outage, "expect_refusals", "outage"),
        spool_service=str(spool.get("service") or ""),
        spool_path=str(spool.get("path") or ""),
        spool_must_stay_empty=_flag(spool, "must_stay_empty", "outage 'spool'"),
        spool_must_replay=_flag(spool, "must_replay", "outage 'spool'"),
        poison_file=str(poison.get("data_file") or ""),
        poison_every=poison_every,
        dead_letter_service=str(dead_letters.get("service") or ""),
        dead_letter_path=str(dead_letters.get("path") or ""),
        archive_services=tuple(str(name) for name in archive_services),
        archive_path=str(archive.get("path") or ""),
        sources={str(source): str(path) for source, path in sources.items()},
    )


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
        started: When the request went out, in seconds since the epoch.
        poison: Whether the record is one the sink refuses.
    """

    seq: int
    source: str
    phase: str
    status: int | None
    error: str
    seconds: float
    started: float = 0.0
    poison: bool = False

    @property
    def answered_2xx(self) -> bool:
        """Whether the receiver took the record."""
        return self.status is not None and 200 <= self.status < 300

    @property
    def accepted(self) -> bool:
        """Whether the receiver took a good record, which obliges it to land."""
        return self.answered_2xx and not (self.poison)

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
    from one part of the outage to the next by assigning it. With `poison`, once
    the outage begins every `poison_every`-th record is one of those instead,
    routed to the first source, so the path is proven before the first one.
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
        poison: Sequence[dict] = (),
        poison_every: int = 0,
    ) -> None:
        """Prepare the workers without sending anything.

        Raises:
            ValueError: If there is nothing to send or nobody to send it.
        """
        if not records:
            raise ValueError("the load has no record to send")
        if workers < 1:
            raise ValueError(f"the load needs at least one worker, got {workers}")
        if poison and poison_every < 2:
            raise ValueError("poison needs good records between, every 2 or more")
        self.phase = PHASES[0]
        self._url = url
        self._prefix = prefix
        self._records = list(records)
        self._poison = list(poison)
        self._poison_every = poison_every if poison else 0
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

    def _claim(self) -> tuple[int, str, dict, bool]:
        with self._lock:
            seq = self._next
            self._next += 1
        every = self._poison_every
        if every and self.phase != PHASES[0] and seq % every == every - 1:
            event = self._poison[(seq // every) % len(self._poison)]
            return seq, self._records[0][0], event, True
        source, event = self._records[seq % len(self._records)]
        return seq, source, event, False

    def _run(self) -> None:
        while not self._stop.is_set():
            seq, source, event, poison = self._claim()
            phase = self.phase
            body = marked_event(
                marker=record_marker(self._prefix, seq), source=source, extra=event
            )
            sent_at = time.time()
            started = time.monotonic()
            status, error = post_record(self._url, body, self._timeout)
            record = Sent(
                seq=seq,
                source=source,
                phase=phase,
                status=status,
                error=error,
                seconds=time.monotonic() - started,
                started=sent_at,
                poison=poison,
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
    """Map each good record of `source` the receiver accepted to the phase it was sent in."""
    return {r.seq: r.phase for r in sent if r.accepted and r.source == source}


def poison_by_seq(sent: Iterable[Sent]) -> dict[int, str]:
    """Map each poison record the receiver accepted to the phase it was sent in."""
    return {r.seq: r.phase for r in sent if r.poison and r.answered_2xx}


def phase_by_seq(sent: Iterable[Sent], source: str) -> dict[int, str]:
    """Map every record of `source` the load sent, answered or not, to its phase."""
    return {r.seq: r.phase for r in sent if r.source == source}


def overlaps(record: Sent, start: float, end: float) -> bool:
    """Whether a request was still open at any moment between `start` and `end`."""
    return record.started <= end and record.started + record.seconds >= start


def refused(record: Sent) -> bool:
    """Whether the receiver answered the request with anything but a 2xx."""
    return record.status is not None and not (record.answered_2xx)


def unanswered(
    sent: Iterable[Sent],
    window: tuple[float, float] | None,
    *,
    in_flight: bool = True,
) -> tuple[list[Sent], list[Sent]]:
    """Split the requests nothing answered into (excused, unexcused).

    `window` runs from the signal to the ingress it went to being healthy again,
    and is None when the ingress stayed up, which excuses nothing. Inside it, a
    request sent after the signal is excused, since nothing was there to answer
    it. One already in flight at the signal is excused only when `in_flight`: a
    kill cannot answer it, a graceful stop has to.
    """
    excused: list[Sent] = []
    unexcused: list[Sent] = []
    for record in sent:
        if record.status is not None:
            continue
        inside = window is not None and overlaps(record, *window)
        if inside and (in_flight or record.started >= window[0]):
            excused.append(record)
        else:
            unexcused.append(record)
    return excused, unexcused


def marker_counts(text: str, prefix: str) -> dict[int, int]:
    """Map each record of this load to its row count, from `marker<TAB>count` lines."""
    counts: dict[int, int] = {}
    for line in text.splitlines():
        marker, _, count = line.strip().partition("\t")
        seq = seq_of(marker, prefix)
        if seq is not None and count.isdigit():
            counts[seq] = counts.get(seq, 0) + int(count)
    return counts


def landed_counts(
    database: str,
    table: str,
    prefix: str,
    *,
    debug: Callable[[str], None] | None = None,
) -> dict[int, int] | None:
    """How many rows each landed record of this load has, or None when no marker can be read.

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
                f"SELECT {expression}, count() FROM {database}.{table} "
                f"WHERE {where} GROUP BY {expression} FORMAT TabSeparated",
                debug=debug,
            )
            return marker_counts(raw, prefix)
        probe = ch_int(
            f"SELECT count() FROM {database}.{table} WHERE {expression} != ''",
            debug=debug,
        )
        readable = readable or bool(probe)
    return {} if readable else None


@dataclass(frozen=True, slots=True)
class Landing:
    """How the records one load sent fared in the table.

    Attributes:
        accepted: How many records the receiver answered 2xx.
        landed: How many of those have at least one row.
        missing: The accepted records with no row, by sequence number.
        lost_by_phase: The missing records, counted by the phase each was sent in.
        duplicated_by_phase: Records with more than one row, counted by phase sent.
        extra_rows: Rows beyond the first, summed over every duplicated record.
    """

    accepted: int
    landed: int
    missing: list[int]
    lost_by_phase: dict[str, int]
    duplicated_by_phase: dict[str, int]
    extra_rows: int

    @property
    def duplicated(self) -> int:
        """How many records landed more than once."""
        return sum(self.duplicated_by_phase.values())


def tally_landing(
    accepted: Mapping[int, str],
    counts: Mapping[int, int],
    phases: Mapping[int, str],
) -> Landing:
    """Judge what landed against what was accepted.

    Args:
        accepted: The accepted records, by sequence number, to the phase sent in.
        counts: Rows per landed record, as `landed_counts` reads them.
        phases: Every record the load sent, to its phase, so a duplicate of a
            refused record is placed too.

    Returns:
        The tally. A duplicate is reported, never judged: at-least-once allows it.
    """
    missing = sorted(set(accepted) - set(counts))
    doubled = {seq: rows for seq, rows in counts.items() if rows > 1}
    return Landing(
        accepted=len(accepted),
        landed=len(accepted) - len(missing),
        missing=missing,
        lost_by_phase=dict(Counter(accepted[seq] for seq in missing)),
        duplicated_by_phase=dict(
            Counter(phases.get(seq, "unknown") for seq in doubled)
        ),
        extra_rows=sum(rows - 1 for rows in doubled.values()),
    )


def grown(before: Mapping[str, int], after: Mapping[str, int]) -> list[str]:
    """The files in `after` that are new since `before`, or larger than they were there."""
    return sorted(path for path, size in after.items() if size > before.get(path, -1))


# The first four bytes of every zstd frame.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def zstd_frames(data: bytes) -> bytes:
    """Return every zstd frame embedded in `data`, decompressed and joined.

    A disk spool frames each record and compresses its body, so the records sit
    between the queue's own headers. Returns empty when this Python has no zstd
    module or `data` holds no frame.
    """
    try:
        from compression import zstd
    except ImportError:
        return b""
    out = []
    start = data.find(ZSTD_MAGIC)
    while start >= 0:
        decompressor = zstd.ZstdDecompressor()
        try:
            out.append(decompressor.decompress(data[start:]))
        except zstd.ZstdError:
            pass
        start = data.find(ZSTD_MAGIC, start + len(ZSTD_MAGIC))
    return b"".join(out)


def count_markers(data: bytes, prefix: str) -> Counter[int]:
    """How many lines of `data` name each record of this load, by sequence number.

    A record can name itself twice, in its marker tag and in the default message
    that carries the marker too, so a line counts once however often it does.
    """
    pattern = re.compile(re.escape(prefix.encode()) + rb"-(\d{6})(?!\d)")
    counts: Counter[int] = Counter()
    for line in data.splitlines():
        counts.update({int(match.group(1)) for match in pattern.finditer(line)})
    return counts


def markers_in(data: bytes, prefix: str) -> set[int]:
    """Every record of this load named anywhere in `data`, by sequence number."""
    return set(count_markers(data, prefix))


def dead_letter_markers(ndjson: str, prefix: str) -> set[int]:
    """Every record of this load in a file dead-letter queue's NDJSON.

    Each line carries the refused record base64-encoded in `payload`. A line that
    is not such an entry is skipped rather than guessed at.
    """
    found: set[int] = set()
    for line in ndjson.splitlines():
        try:
            entry = json.loads(line)
            payload = base64.b64decode(entry["payload"], validate=True)
        except (ValueError, KeyError, TypeError, binascii.Error):
            continue
        found |= markers_in(payload, prefix)
    return found


def metric_total(exposition: str, name: str) -> float | None:
    """Sum every sample of a Prometheus metric whose name ends in `name`, else None.

    A service prefixes the metrics its library registers with its own namespace,
    so `dfe_loader_pipeline_dead_letters_dropped_total` counts as one of `name`.
    """
    total = None
    for line in exposition.splitlines():
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            series, rest = line[: line.index("{")], line[line.rindex("}") + 1 :]
        else:
            series, _, rest = line.partition(" ")
        if series != name and not series.endswith(f"_{name}"):
            continue
        try:
            value = float(rest.split()[0])
        except (IndexError, ValueError):
            continue
        total = (total or 0.0) + value
    return total


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
        exit_code: How its last process exited, 0 while none has.
    """

    container_id: str
    status: str
    health: str
    restarts: int
    started_at: str
    pid: int
    exit_code: int = 0


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
        exit_code=int(state.get("ExitCode", 0) or 0),
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


def pattern_subscriptions(regex: str, topics: Iterable[str]) -> set[str]:
    """The topics a `topic_regex` subscriber reads out of `topics`.

    The pattern is searched rather than anchored, as scalo's topic include is,
    and a `<base>_land` drops out wherever `<base>_load` matched too: scalo reads
    each source once, at its transform's output.
    """
    pattern = re.compile(regex)
    matched = {topic for topic in topics if pattern.search(topic)}
    return {
        topic
        for topic in matched
        if not (topic.endswith("_land") and f"{topic[: -len('_land')]}_load" in matched)
    }


def expected_consumers(
    configs: Iterable[Path], topics: Iterable[str] = ()
) -> Counter[str]:
    """Count the services each topic is a subscription of, across the configs.

    A config that names no topics and subscribes by `topic_regex` counts on each
    of `topics` its pattern reads, so a pattern subscriber is owed a commit too.
    """
    known = list(topics)
    counts: Counter[str] = Counter()
    for path in configs:
        subscribed, _, regex = _config_topics(path=path)
        if not (subscribed) and regex:
            subscribed = pattern_subscriptions(regex, known)
        counts.update(subscribed)
    return counts


def high_watermark_text(described: str) -> int | None:
    """Sum a topic's high watermarks from `rpk topic describe <topic> -p`, else None.

    None when no partition row can be read, so an error or an unknown topic is
    never mistaken for an empty one.
    """
    column = None
    total = None
    for line in described.splitlines():
        cells = line.split()
        if "HIGH-WATERMARK" in cells:
            column = cells.index("HIGH-WATERMARK")
            continue
        if column is None or len(cells) <= column or not cells[column].isdigit():
            continue
        total = (total or 0) + int(cells[column])
    return total


def high_watermark_offsets(described: str, topic: str) -> int | None:
    """Sum a topic's latest offsets from Apache Kafka's `kafka-get-offsets.sh`, else None.

    That tool prints one `<topic>:<partition>:<offset>` line per partition.
    """
    total = None
    for line in described.splitlines():
        cells = line.strip().rsplit(":", 2)
        if len(cells) != 3 or cells[0] != topic or not cells[2].isdigit():
            continue
        total = (total or 0) + int(cells[2])
    return total


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


def consumers_uncommitted(
    lag: Mapping[str, Mapping[str, int]], expected: Mapping[str, int]
) -> list[str]:
    """Name each expected topic with fewer committing groups than services subscribe to it."""
    problems = []
    for topic, wanted in sorted(expected.items()):
        groups = sorted(group for group, topics in lag.items() if topic in topics)
        if len(groups) < wanted:
            problems.append(
                f"'{topic}': {len(groups)} of {wanted} consumer group(s) have committed "
                f"({', '.join(groups) or 'none'})"
            )
    return problems


def kafka_unreached(
    watermarks: Mapping[str, int | None],
    lag: Mapping[str, Mapping[str, int]],
    expected: Mapping[str, int],
) -> list[str]:
    """Say why the load has not been shown to travel through Kafka yet.

    Each landing topic in `watermarks` needs a record on it, and each expected
    topic its committing groups. Empty means the path runs through the broker, so
    stopping it tests what the outage claims to.
    """
    problems = []
    for topic, mark in sorted(watermarks.items()):
        if mark is None:
            problems.append(f"'{topic}': high watermark unreadable")
        elif mark < 1:
            problems.append(f"'{topic}': never reached Kafka (high watermark {mark})")
    return problems + consumers_uncommitted(lag, expected)


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
    problems = consumers_uncommitted(lag, expected)
    for topic in sorted(set(expected) | set(watched)):
        for group in sorted(lag):
            behind = lag[group].get(topic, 0)
            if behind:
                problems.append(f"'{topic}': group '{group}' is {behind} behind")
    return problems
