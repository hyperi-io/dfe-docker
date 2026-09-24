#  Project:      dfe-docker
#  File:         _pipeline.py
#  Purpose:      Shared primitives for proving events traverse the pipeline
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Shared "did the event land?" primitives.

Internal support module - imported by the e2e suite and the power-on self test,
not executed directly.

Both of those answer the same question (inject a marked event, prove that exact
event reached ClickHouse) and differ only in who owns the stack: the e2e suite
stands one up per test, the self test runs against whatever is already up. The
part they share is the interesting part, and it is subtle enough - marker
escaping, distinguishing "no rows" from "cannot read markers", polling instead of
sleeping - that a second copy would drift into a second set of bugs.
"""

from __future__ import annotations

import json
import os
import re
import time
import typing
from urllib.error import URLError
from urllib.request import Request, urlopen

# How to read a run's marker back out of a landed row.
#
# Events are stamped with `_tags = {"marker": ...}`. In the engine-provisioned
# schema `_tags` is Nullable(JSON), so `_tags.marker` is the intended access and
# yields a Dynamic that needs toString() to compare. The JSONExtractString form is
# the fallback for a deployment where `_tags` landed as a plain String.
#
# This repo does not own that schema, so both forms are tried rather than one
# being assumed.
MARKER_EXPRESSIONS = (
    "toString(`_tags`.marker)",
    "JSONExtractString(toString(`_tags`), 'marker')",
)


def env_or(name: str, fallback: str) -> str:
    """Read an env var, treating empty as absent.

    A `.env` key present with no value exports as an empty string, so a bare
    ``os.environ.get(name, default)`` returns '' and the default never fires.
    """
    return os.environ.get(name, "").strip() or fallback


_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(text: str) -> str:
    """Replace each ``${NAME}`` or ``${NAME:-default}`` with its environment value.

    Empty reads as unset, as it does in :func:`env_or`, so a test definition
    follows the same port variables compose publishes the stack on.
    """
    return _ENV_REFERENCE.sub(
        lambda match: env_or(match.group(1), match.group(2) or ""), text
    )


def clickhouse_url() -> str:
    """Return the ClickHouse HTTP endpoint, honouring the same vars compose uses."""
    explicit = os.environ.get("CLICKHOUSE_URL", "").strip()
    if explicit:
        return explicit
    return f"http://localhost:{env_or('CLICKHOUSE_HTTP_PORT', '8123')}"


def http_get(url: str, timeout: int = 5) -> str:
    """GET a URL and return the body. Raises on any transport error."""
    with urlopen(Request(url, method="GET"), timeout=timeout) as response:
        return response.read().decode()


def _request(
    *,
    method: str,
    url: str,
    content_type: str = "",
    data: bytes | None = None,
    timeout: int,
    token: str = "",
) -> tuple[int, str]:
    """Make one request and return (status, body text).

    A response carrying an HTTP error status is a RESULT here, not an exception:
    every caller reports the status it got. A transport failure with no status at
    all still raises, because there is nothing to report.
    """
    request = Request(url, data=data, method=method)
    if content_type:
        request.add_header("Content-Type", content_type)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except URLError as error:
        if not (hasattr(error, "code")):
            raise
        return error.code, error.read().decode() if hasattr(error, "read") else ""


def _decoded(raw: str) -> typing.Any:
    """Decode a JSON body, or return the raw text when it is not JSON.

    A body that is not JSON comes back as the raw text, so a proxy error page is
    reported as what it is rather than raising a decode error over the top of it.
    """
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def http_get_json(
    url: str, token: str = "", timeout: int = 30
) -> tuple[int, typing.Any]:
    """GET and return (status, decoded body), for callers that need the body."""
    status, raw = _request(method="GET", url=url, timeout=timeout, token=token)
    return status, _decoded(raw)


def http_post_json(
    url: str,
    payload: dict,
    token: str = "",
    timeout: int = 30,
) -> tuple[int, typing.Any]:
    """POST JSON and return (status, decoded body), for callers that need the body."""
    status, raw = _request(
        method="POST",
        url=url,
        content_type="application/json",
        data=json.dumps(payload).encode(),
        timeout=timeout,
        token=token,
    )
    return status, _decoded(raw)


def http_post(
    url: str,
    body: str | bytes,
    content_type: str = "application/json",
    timeout: int = 10,
) -> int:
    """POST a body and return the HTTP status."""
    return _request(
        method="POST",
        url=url,
        content_type=content_type,
        data=body.encode() if isinstance(body, str) else body,
        timeout=timeout,
    )[0]


def http_delete(url: str, token: str = "", timeout: int = 10) -> int:
    """DELETE a resource and return the HTTP status.

    Used to put back what a self test created. It reports the status rather than
    raising, because tidy-up runs on the failure path too and a second exception
    there would bury the finding that mattered.
    """
    return _request(method="DELETE", url=url, timeout=timeout, token=token)[0]


def ch_query(sql: str, *, debug: typing.Callable[[str], None] | None = None) -> str:
    """Run a query over ClickHouse's HTTP interface, returning '' on any failure.

    Returning '' rather than raising is deliberate: callers here are probing a
    schema they do not own, so "that query did not work" is an expected answer
    that has to be distinguishable from a real result.
    """
    if debug:
        debug(f"Executing ClickHouse query: `{sql}`")
    request = Request(clickhouse_url(), data=sql.encode(), method="POST")
    request.add_header("X-ClickHouse-User", env_or("CLICKHOUSE_USERNAME", "default"))
    request.add_header("X-ClickHouse-Key", os.environ.get("CLICKHOUSE_PASSWORD", ""))
    try:
        with urlopen(request, timeout=10) as response:
            return response.read().decode().strip()
    except Exception as error:  # noqa: BLE001 - any failure is "could not query"
        if debug:
            debug(f"ClickHouse query failed. Error message: {error}")
        return ""


def ch_count(database: str, table: str, *, debug=None) -> int:
    """Return the row count of a table, or 0 if it cannot be read."""
    count = ch_int(f"SELECT count() FROM {database}.{table}", debug=debug)
    return 0 if count is None else count


def ch_int(sql: str, *, debug=None) -> int | None:
    """Run a single-value query and return it as an int, or None if unreadable.

    None means the query did not run - a missing table, a column the schema does
    not carry, an unreachable server. A caller waiting for rows to appear needs
    that separate from a real zero, because only one of the two is worth waiting
    on.
    """
    raw = ch_query(sql, debug=debug)
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return None


def escape_literal(value: str) -> str:
    """Escape a value for use inside a single-quoted ClickHouse string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def ch_marker_count(
    database: str, table: str, marker: str, *, debug=None
) -> int | None:
    """Count rows carrying this run's marker.

    Returns the count, or None when no expression could read a marker at all -
    "I could not check" must stay distinguishable from "nothing arrived", because
    the two call for opposite reactions.
    """
    escaped = escape_literal(marker)
    usable = False

    for expression in MARKER_EXPRESSIONS:
        raw = ch_query(
            f"SELECT count() FROM {database}.{table} WHERE {expression} = '{escaped}'",
            debug=debug,
        )
        try:
            matched = int(raw.strip())
        except (ValueError, AttributeError):
            # Query errored, so this expression does not fit the schema.
            continue

        if matched > 0:
            return matched

        # Zero is the dangerous answer: a query that RUNS is not a query that
        # WORKS. If `_tags` were a Map rather than JSON, the JSONExtractString
        # fallback succeeds and returns '' for every row -- every comparison is
        # false, the count is a clean 0, and we would report data loss that never
        # happened. Prove the expression can read SOMEBODY's marker first.
        probe = ch_query(
            f"SELECT count() FROM {database}.{table} WHERE {expression} != ''",
            debug=debug,
        )
        try:
            if int(probe.strip()) > 0:
                usable = True
        except (ValueError, AttributeError):
            pass

    return 0 if usable else None


# Tables the collector's ClickHouse exporter creates, with the column carrying
# event time. Metrics lead: scalo's OTLP backend is a metrics backend, so logs
# and traces are probed but not expected to carry rows.
OTEL_FRESHNESS_TABLES = (
    ("otel_metrics_sum", "TimeUnix"),
    ("otel_metrics_gauge", "TimeUnix"),
    ("otel_metrics_histogram", "TimeUnix"),
    ("otel_logs", "Timestamp"),
    ("otel_traces", "Timestamp"),
)


def otel_fresh_counts(
    database: str, window_seconds: int, *, debug=None
) -> dict[str, int] | None:
    """Count self-telemetry rows newer than the window, per table.

    Freshness rather than existence, so the assertion is that the pipeline is
    streaming now. Returns None when no table could be read: an absent schema and
    an empty one call for opposite reactions.
    """
    counts: dict[str, int] = {}
    for table, column in OTEL_FRESHNESS_TABLES:
        raw = ch_query(
            f"SELECT count() FROM {database}.{table} "
            f"WHERE {column} > now() - INTERVAL {int(window_seconds)} SECOND",
            debug=debug,
        )
        try:
            counts[table] = int(raw.strip())
        except (ValueError, AttributeError):
            continue
    return counts or None


def marked_event(*, marker: str, source: str, extra: dict | None = None) -> str:
    """Build one JSON event stamped with `marker`, routed to `source`.

    The loader routes an event to <default_db>.<_source>, and the marker rides in
    `_tags` where ch_marker_count reads it back.
    """
    event = dict(extra or {})
    event["_source"] = source
    event.setdefault("message", f"dfe pipeline check {marker}")
    tags = dict(event.get("_tags") or {})
    tags["marker"] = marker
    event["_tags"] = tags
    return json.dumps(event, separators=(",", ":"))


def poll_until(
    predicate: typing.Callable[[], typing.Any],
    *,
    timeout: float,
    interval: float,
    done: typing.Callable[[typing.Any], bool] | None = None,
    on_attempt: typing.Callable[[int, typing.Any], None] | None = None,
) -> typing.Any:
    """Poll until `done(result)` holds, or the deadline passes. Returns the last result.

    `done` defaults to plain truthiness, which is WRONG for counts and the caller
    must say so: a partially-landed batch returns 1 of an expected 3, which is
    truthy, so a truthiness test stops on the first row and reports a failure that
    a moment's more waiting would have resolved. Anything counting things should
    pass an explicit `done`.

    Polling rather than sleeping a fixed amount: the pipeline is asynchronous, so
    a fixed sleep is either slower than it needs to be or flaky, usually both on
    different machines. The timeout is the stuck-dependency backstop, not the
    expected duration.
    """
    settled = done or bool
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        result = predicate()
        if on_attempt:
            on_attempt(attempt, result)
        if settled(result):
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)
