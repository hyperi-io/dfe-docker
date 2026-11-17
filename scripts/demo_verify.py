#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/demo_verify.py
#  Purpose:      Prove a published demo pack worked end to end, not only that its stack came up and rows arrived
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Check a demo pack's published stack did its job, from the host that started it.

CI runs it in each "Published compose" leg once the stack is healthy:

  demo_verify.py --pack demo-cyber --file dfe-demo-cyber.yaml

Every call into the stack goes through one `docker compose exec` into dfe-engine, which logs in from the engine's own secrets file and makes the requests there, so no credential reaches this process or the runner's log. SQL runs through the engine's query API, the one dfe-ui's search calls. The checks:

  landed     every source whose events the pack replays holds rows
  fetchers   each fetcher source whose polls the pack replays holds rows, and the others say why they are skipped
  parsed     no row carries `error.message` or a `pipeline_error` tag, and each field the pack's verify.json names is set on at least its `min_ratio` of rows
  unclaimed  the intake's own table and the default table hold nothing
  sources    every pack source is deployed and in the state its file declares
  hunts      each rule of each hunt the pack ships wrote detections, and the hunt wrote no more than its `max_alerts`
  console    every dashboard and saved search the pack names is installed and its queries return rows
  search     a saved search's query, written as the console sends it, runs through the engine's query API as the console login, and returns rows unless zero_ok names it
  dlq        dfe.dlq and each dead-letter topic exist and hold nothing

What a pack expects is data in resources/<pack>/verify.json:

  parsed   {<source>: {"fields": [<column or _json.path>, ...], "min_ratio": 0.95}}
  hunts    {<hunt>: {"max_alerts": <ceiling over the run>}}, which is six five-minute runs of the stack's cap (publish_compose.DEMO_MAX_DETECTIONS) plus the summary row each run writes past it
  waiting  {"reason": <text>, "sources": [<source>, ...]} sources that cannot land in their own table until a fix ships, which the landing, fetcher, parsed and unclaimed checks leave out and say so
  zero_ok  ["<dashboard>/<tile id>" or "<saved search>", ...] queries that return no rows or read 0 on purpose

Exit codes:
  0  every check held
  1  a check failed, naming the table, the count and the query
  2  usage error
"""

import argparse
import json
import re
import subprocess
import sys
import time
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

from _common import _print, demo_pack_dir  # noqa: E402

DATABASE = "dfe"
DEFAULT_TABLE = "main"
DETECTION_TABLE = "detection"
DLQ_TABLE = "dlq"
ENGINE_SERVICE = "dfe-engine"
KAFKA_SERVICE = "kafka-redpanda"
ENGINE_API = "http://localhost:8000/api/v1"
HYPERDX_API = "http://dfe-hyperdx:8000"
DATASOURCE = "clickhouse:default"
BREAKGLASS_USER = "breakglass"
BREAKGLASS_PASSWORD_KEY = "DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD"
SECRETS_FILE = "/run/dfe-secrets/env"
DLQ_TOPICS = (
    "dfe_archiver_dlq",
    "dfe_fetcher_dlq",
    "dfe_loader_dlq",
    "dfe_receiver_dlq",
    "dfe_transform_dlq",
)
VERIFY_FILE = "verify.json"
DEFAULT_MIN_RATIO = 0.95
CONSOLE_RANGE_SECONDS = 3600
CONSOLE_INTERVAL_SECONDS = 60
LAND_TIMEOUT_SECONDS = 300.0
FETCH_TIMEOUT_SECONDS = 600.0
HUNT_TIMEOUT_SECONDS = 900.0
# The archived tile reads zero until dfe-archiver closes its first file, 300 s after it opens on Kafka, and its counter exports after that
CONSOLE_TIMEOUT_SECONDS = 480.0
POLL_INTERVAL_SECONDS = 5.0
EXEC_TIMEOUT_SECONDS = 240
CHECKS = (
    "landed",
    "fetchers",
    "parsed",
    "unclaimed",
    "sources",
    "hunts",
    "console",
    "search",
    "dlq",
)
# A Source file's own first-level `state:` and the intake's agentless mode
STATE_LINE = re.compile(r"^state:\s*(\w+)\s*$", re.MULTILINE)
AGENTLESS_LINE = re.compile(r"^\s+mode:\s*agentless\s*$", re.MULTILINE)
FETCHER_LINE = re.compile(r"^fetcher:", re.MULTILINE)
RULE_LINE = re.compile(r"^\s+-\s+(\S+)\s*$")
FIELD_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
TABLE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
# What a column holds when it is empty: an unset field, an empty array or object
EMPTY_VALUES = ("", "[]", "{}", "null")
ERROR_ROW = (
    "(JSONHas(toJSONString(_json), 'error', 'message') "
    "OR position(toJSONString(_tags), 'pipeline_error') > 0)"
)

# Runs in dfe-engine: logs in as each identity a request names, once, replacing an issued password first as a person's first use of the console login does, makes each request and prints the answers as one JSON line
ENGINE_SCRIPT = (
    "import json, os, secrets, sys, urllib.error, urllib.request\n"
    "base = sys.argv[1]\n"
    "def call(method, url, payload, token):\n"
    "    body = None if payload is None else json.dumps(payload).encode()\n"
    "    request = urllib.request.Request(url, data=body, method=method)\n"
    "    request.add_header('Content-Type', 'application/json')\n"
    "    if token:\n"
    "        request.add_header('Authorization', 'Bearer ' + token)\n"
    "    try:\n"
    "        with urllib.request.urlopen(request, timeout=120) as answer:\n"
    "            raw, status = answer.read().decode(), answer.status\n"
    "    except urllib.error.HTTPError as error:\n"
    "        raw, status = error.read().decode(), error.code\n"
    "    try:\n"
    "        return [status, json.loads(raw)]\n"
    "    except ValueError:\n"
    "        return [status, raw]\n"
    "def login(username, password):\n"
    "    status, body = call('POST', base + '/auth/login', {'username': username, 'password': password}, '')\n"
    "    token = body.get('access_token', '') if isinstance(body, dict) else ''\n"
    "    if token and body.get('password_change_required') is True:\n"
    "        fresh = secrets.token_hex(16)\n"
    "        status, body = call('POST', base + '/auth/accounts/reset-password', {'current_password': password, 'new_password': fresh}, token)\n"
    "        if status != 200:\n"
    "            return ''\n"
    "        status, body = call('POST', base + '/auth/login', {'username': username, 'password': fresh}, '')\n"
    "        token = body.get('access_token', '') if isinstance(body, dict) else ''\n"
    "    return token\n"
    "spec = json.load(sys.stdin)\n"
    "tokens = {}\n"
    "def token_for(identity):\n"
    "    if identity not in tokens:\n"
    "        if identity == 'console' and spec.get('console_login'):\n"
    "            tokens[identity] = login(spec['console_login']['username'], spec['console_login']['password'])\n"
    "        elif identity == 'breakglass':\n"
    "            tokens[identity] = login(spec['breakglass_user'], os.environ[spec['breakglass_key']])\n"
    "        else:\n"
    "            tokens[identity] = ''\n"
    "    return tokens[identity]\n"
    "print(json.dumps([call(item['method'], item['url'], item.get('payload'), token_for(item['as'])) for item in spec['requests']]))\n"
)


class VerifyError(Exception):
    """A pack or stack this checker cannot read, with what to change."""


@dataclass(frozen=True, slots=True)
class Request:
    """One call made inside the engine's container, as `breakglass` or the minted `console` login."""

    method: str
    url: str
    payload: dict | None = None
    identity: str = "breakglass"


@dataclass(frozen=True, slots=True)
class Reply:
    """What a request answered: its HTTP status and decoded body."""

    status: int
    body: typing.Any


@dataclass(frozen=True, slots=True)
class Failure:
    """One failed check, saying what failed where."""

    check: str
    detail: str
    # False where waiting cannot change the answer, such as an alert count already over its ceiling
    retry: bool = True


@dataclass(frozen=True, slots=True)
class Result:
    """One check's outcome: what held, what failed or why it was skipped."""

    check: str
    detail: str
    failures: tuple[Failure, ...] = ()
    skipped: bool = False


@dataclass(slots=True)
class Pack:
    """A demo pack's files, read without YAML so a bare python3 can run this.

    Attributes:
        name: The pack's directory name, `demo` or `demo-<pack>`.
        sources: The sources demo.json lists.
        states: Each source's `state:` in its file, active where it declares none.
        replayed: Sources whose events the pack sends at the receiver.
        polled: Fetcher sources whose captured polls the pack serves.
        fetchers: Every source that is a fetcher.
        intake: The agentless intake's source name, None for a pack with no syslog.
        dashboards: Names of the dashboards demo.json lists.
        searches: Names of the saved searches demo.json lists.
        hunts: Each hunt demo.json lists, with the rules its file runs.
        parsed: verify.json's expected fields per source.
        hunt_limits: verify.json's alert ceiling per hunt.
        zero_ok: verify.json's tiles and searches that return no rows or read 0 on purpose.
        waiting_sources: verify.json's sources whose rows cannot land in their own table yet, left out of the landing checks.
        waiting_reason: Why they cannot, which the skipped checks print.
    """

    name: str
    sources: list[str]
    states: dict[str, str]
    replayed: list[str]
    polled: list[str]
    fetchers: list[str]
    intake: str | None
    dashboards: list[str]
    searches: list[str]
    hunts: dict[str, list[str]]
    parsed: dict[str, dict]
    hunt_limits: dict[str, dict]
    zero_ok: set[str] = field(default_factory=set)
    waiting_sources: list[str] = field(default_factory=list)
    waiting_reason: str = ""


class Stack(typing.Protocol):
    """A running stack: a way into dfe-engine, the broker's dead-letter topics and the compose file's services."""

    def calls(self, requests: Sequence[Request]) -> list[Reply]: ...

    def services(self) -> list[str]: ...

    def topic_marks(self, topics: Sequence[str]) -> dict[str, int | None]: ...


@dataclass(frozen=True, slots=True)
class Compose:
    """A stack reached through `docker compose -f <file>`, with the console login it holds for the search check."""

    file: Path
    context: str = ""
    console_login: tuple[str, str] | None = None
    login_fault: str = ""

    def _compose(self, *args: str) -> list[str]:
        context = ["--context", self.context] if self.context else []
        return ["docker", *context, "compose", "-f", str(self.file), *args]

    def _run(self, command: list[str], *, stdin: str | None = None):
        return subprocess.run(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            input=stdin,
            timeout=EXEC_TIMEOUT_SECONDS,
        )

    def calls(self, requests: Sequence[Request]) -> list[Reply]:
        """Send every request in one exec into the engine and return the answers in order."""
        login = self.console_login
        spec = {
            "breakglass_key": BREAKGLASS_PASSWORD_KEY,
            "breakglass_user": BREAKGLASS_USER,
            "console_login": (
                {"username": login[0], "password": login[1]} if login else None
            ),
            "requests": [
                {
                    "as": item.identity,
                    "method": item.method,
                    "payload": item.payload,
                    "url": item.url,
                }
                for item in requests
            ],
        }
        result = self._run(
            self._compose(
                "exec",
                "-T",
                ENGINE_SERVICE,
                "sh",
                "-c",
                f'set -a && . {SECRETS_FILE} && python -c "$0" {ENGINE_API}',
                ENGINE_SCRIPT,
            ),
            stdin=json.dumps(spec),
        )
        if result.returncode != 0:
            raise VerifyError(
                f"{ENGINE_SERVICE} exec failed with {result.returncode}: "
                f"{result.stderr.strip()[-400:]}"
            )
        try:
            answers = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as error:
            raise VerifyError(
                f"{ENGINE_SERVICE} answered no JSON: {result.stdout.strip()[-400:]!r}"
            ) from error
        return [Reply(status=status, body=body) for status, body in answers]

    def services(self) -> list[str]:
        """The compose file's service names."""
        result = self._run(self._compose("config", "--services"))
        return result.stdout.split() if result.returncode == 0 else []

    def topic_marks(self, topics: Sequence[str]) -> dict[str, int | None]:
        """Each topic's high watermark summed over its partitions, None where the broker cannot say."""
        marks: dict[str, int | None] = {}
        for topic in topics:
            result = self._run(
                self._compose(
                    "exec", "-T", KAFKA_SERVICE, "rpk", "topic", "describe", topic, "-p"
                )
            )
            marks[topic] = (
                watermark_total(text=result.stdout) if result.returncode == 0 else None
            )
        return marks

    def with_minted_login(self) -> "Compose":
        """This stack with the console login secrets-init mints, held in memory and never printed."""
        result = self._run(self._compose("run", "--rm", "secrets-init", "login"))
        login = parse_login(text=result.stdout) if result.returncode == 0 else None
        fault = ""
        if login is None:
            # A line holding the password is the only one that can match, so none that did not is printed
            fault = (
                f"secrets-init login exited {result.returncode} and printed no 'Console login: <name> / <password>' line. "
                f"Its stderr ends: {result.stderr.strip()[-300:]!r}"
            )
        return Compose(
            file=self.file,
            context=self.context,
            console_login=login,
            login_fault=fault,
        )


def parse_login(*, text: str) -> tuple[str, str] | None:
    """The name and password in secrets-init's `Console login: <name> / <password>` line."""
    found = re.search(r"^Console login: (.+?) / (\S+)$", text, re.MULTILINE)
    return (found.group(1), found.group(2)) if found else None


def watermark_total(*, text: str) -> int | None:
    """The sum of the HIGH-WATERMARK column of `rpk topic describe -p`, None when it has no such column."""
    lines = [line.split() for line in text.strip().splitlines()]
    if len(lines) < 2 or "HIGH-WATERMARK" not in lines[0]:
        return None
    column = lines[0].index("HIGH-WATERMARK")
    try:
        return sum(int(row[column]) for row in lines[1:])
    except (IndexError, ValueError):
        return None


def table_ref(*, table: str) -> str:
    """The `dfe`.`<table>` reference, refusing a name that could change the query."""
    if not TABLE_NAME.fullmatch(table):
        raise VerifyError(f"{table!r} is not a table name")
    return f"{DATABASE}.`{table}`"


def present(*, name: str) -> str:
    """A ClickHouse condition that is true where the column or `_json.` path holds a value."""
    if not FIELD_PATH.fullmatch(name):
        raise VerifyError(f"{name!r} is not a column or a `_json.` path")
    values = ", ".join(f"'{empty}'" for empty in EMPTY_VALUES)
    return f"ifNull(toString({name}) NOT IN ({values}), 0)"


def coverage_query(*, fields: Sequence[str], table: str) -> str:
    """One row: the table's rows, those carrying an error, then the rows holding each field in order."""
    cells = [f"countIf({present(name=name)})" for name in fields]
    columns = ["count()", f"countIf({ERROR_ROW})", *cells]
    return f"SELECT {', '.join(columns)} FROM {table_ref(table=table)}"


def expand_macros(*, sql: str) -> str:
    """A dashboard tile's SQL with the console's time macros filled in for the last CONSOLE_RANGE_SECONDS."""
    window = f"INTERVAL {CONSOLE_RANGE_SECONDS} SECOND"
    sql = re.sub(
        r"\$__timeFilter\(([^)]*)\)",
        lambda found: (
            f"({found.group(1)} >= now() - {window} AND {found.group(1)} <= now())"
        ),
        sql,
    )
    sql = re.sub(
        r"\$__timeInterval\(([^)]*)\)",
        lambda found: (
            f"toStartOfInterval({found.group(1)}, INTERVAL {CONSOLE_INTERVAL_SECONDS} SECOND)"
        ),
        sql,
    )
    return sql.replace("$__interval_s", str(CONSOLE_INTERVAL_SECONDS))


# A `_json.` path the console has not already wrapped in toString
BARE_JSON_PATH = re.compile(r"(?<!toString\()\b_json((?:\.[A-Za-z_][A-Za-z0-9_]*)+)")


def console_form(*, expression: str) -> str:
    """A saved search's select or where as the console sends it, each bare `_json.` path wrapped in toString.

    The console's ClickHouse proxy does that, so a function that needs the path as a Dynamic (`dynamicElement`) errors there and not through the engine's query API.
    """
    return BARE_JSON_PATH.sub(r"toString(_json\1)", expression)


def search_query(*, search: dict, table: str) -> str:
    """The count a saved search's select and where read from its table, which errors where either is wrong."""
    where = console_form(expression=str(search.get("where") or "").strip() or "1")
    select = console_form(expression=str(search.get("select") or "").strip() or "*")
    return (
        f"SELECT count() FROM (SELECT {select} FROM {table_ref(table=table)} "
        f"WHERE {where} LIMIT 100)"
    )


def first_number(*, rows: Sequence[dict]) -> float | None:
    """The first cell of the first row as a number, None when there is none or it is not one."""
    if not rows:
        return None
    try:
        return float(next(iter(rows[0].values())))
    except (StopIteration, TypeError, ValueError):
        return None


def rows_of(*, reply: Reply) -> list[dict] | None:
    """The rows a query answered, None when it did not answer 200 with a `rows` list."""
    if reply.status != 200 or not isinstance(reply.body, dict):
        return None
    rows = reply.body.get("rows")
    return rows if isinstance(rows, list) else None


def fault_of(*, reply: Reply) -> str:
    """Why a query did not answer, from the engine's problem body."""
    body = reply.body
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return f"HTTP {reply.status}: {str(detail)[:300]}"


def sql_request(*, sql: str, identity: str = "breakglass") -> Request:
    """The engine query API call that runs `sql` against the data database."""
    return Request(
        identity=identity,
        method="POST",
        payload={"datasource": DATASOURCE, "query": sql},
        url=f"{ENGINE_API}/queries/raw",
    )


def run_sql(
    *, queries: Sequence[str], stack: Stack, identity: str = "breakglass"
) -> list[Reply]:
    """Run every query in one exec and return each answer."""
    return stack.calls([sql_request(sql=sql, identity=identity) for sql in queries])


def count_tables(*, stack: Stack, tables: Sequence[str]) -> dict[str, int | None]:
    """The rows in each table, None where the count could not be read."""
    queries = [f"SELECT count() FROM {table_ref(table=table)}" for table in tables]
    counts: dict[str, int | None] = {}
    for table, reply in zip(tables, run_sql(queries=queries, stack=stack), strict=True):
        rows = rows_of(reply=reply)
        number = first_number(rows=rows) if rows else None
        counts[table] = None if number is None else int(number)
    return counts


def wait_for(
    *,
    probe: Callable[[], Result],
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Run `probe` again every POLL_INTERVAL_SECONDS until it holds or `timeout` has passed, and return the last result."""
    deadline = clock() + timeout
    while True:
        result = probe()
        waitable = any(failure.retry for failure in result.failures)
        if not waitable or clock() >= deadline:
            return result
        sleep(POLL_INTERVAL_SECONDS)


def check_landed(*, pack: Pack, stack: Stack) -> Result:
    """Every source whose events the pack replays holds rows."""
    if not pack.replayed:
        return Result("landed", f"{pack.name} replays no source's events", skipped=True)
    counts = count_tables(stack=stack, tables=pack.replayed)
    failures = tuple(
        Failure(
            "landed",
            f"{DATABASE}.{table} holds {'an unreadable count' if rows is None else f'{rows} rows'}. "
            f"Query: SELECT count() FROM {table_ref(table=table)}",
        )
        for table, rows in counts.items()
        if not rows
    )
    return Result(
        "landed",
        ", ".join(f"{table} {rows}" for table, rows in counts.items()),
        failures,
    )


def check_parsed(*, pack: Pack, stack: Stack) -> Result:
    """No replayed or polled row carries an error and each expected field is set on at least its ratio of rows."""
    tables = [*pack.replayed, *pack.polled]
    if not tables:
        return Result("parsed", f"{pack.name} replays no source's events", skipped=True)
    plans = []
    for table in tables:
        expected = pack.parsed.get(table, {})
        fields = list(expected.get("fields", []))
        plans.append(
            (table, fields, float(expected.get("min_ratio", DEFAULT_MIN_RATIO)))
        )
    queries = [coverage_query(fields=fields, table=table) for table, fields, _ in plans]
    failures: list[Failure] = []
    held = []
    for (table, fields, ratio), sql, reply in zip(
        plans, queries, run_sql(queries=queries, stack=stack), strict=True
    ):
        rows = rows_of(reply=reply)
        if not rows:
            failures.append(
                Failure(
                    "parsed",
                    f"{table_ref(table=table)} could not be read ({fault_of(reply=reply)}). Query: {sql}",
                )
            )
            continue
        cells = [int(cell) for cell in rows[0].values()]
        total, errored, set_counts = cells[0], cells[1], cells[2:]
        if total == 0:
            # A fetcher table with no rows is the fetchers check's failure to name
            if table not in pack.polled:
                failures.append(
                    Failure(
                        "parsed", f"{table_ref(table=table)} holds 0 rows. Query: {sql}"
                    )
                )
            continue
        if errored:
            failures.append(
                Failure(
                    "parsed",
                    f"{table_ref(table=table)}: {errored} of {total} rows carry error.message "
                    f"or a pipeline_error tag. Query: SELECT substring(toJSONString(_json), 1, 300) "
                    f"FROM {table_ref(table=table)} WHERE {ERROR_ROW} LIMIT 3",
                )
            )
        for name, number in zip(fields, set_counts, strict=True):
            if number < ratio * total:
                failures.append(
                    Failure(
                        "parsed",
                        f"{table_ref(table=table)}: {name} is set on {number} of {total} rows "
                        f"({number / total:.0%}), under the {ratio:.0%} verify.json expects. "
                        f"Query: SELECT countIf({present(name=name)}) FROM {table_ref(table=table)}",
                    )
                )
        held.append(f"{table} {total}")
    return Result("parsed", ", ".join(held), tuple(failures))


def check_unclaimed(*, pack: Pack, stack: Stack) -> Result:
    """The intake's own table and the default table hold nothing, bar rows of a source waiting on a fix, and no waiting source has its own rows yet."""
    # A waiting source lands in the default table until the fix ships, so only its rows are left out of the count
    leave_out = ""
    for source in pack.waiting_sources:
        table_ref(table=source)
        leave_out += f" AND NOT startsWith(toString(_source), '{source}')"
    tables = [*([pack.intake] if pack.intake else []), DEFAULT_TABLE]
    queries = [
        f"SELECT count() FROM {table_ref(table=table)}"
        + (f" WHERE 1{leave_out}" if table == DEFAULT_TABLE else "")
        for table in tables
    ]
    waiting = [
        f"SELECT count() FROM {table_ref(table=s)}" for s in pack.waiting_sources
    ]
    replies = run_sql(queries=[*queries, *waiting], stack=stack)
    counts: dict[str, int | None] = {}
    for table, reply in zip(tables, replies, strict=False):
        rows = rows_of(reply=reply)
        number = first_number(rows=rows) if rows else None
        counts[table] = None if number is None else int(number)
    failures = []
    for (table, rows), sql in zip(counts.items(), queries, strict=True):
        if rows == 0:
            continue
        why = (
            "its count could not be read"
            if rows is None
            else f"holds {rows} rows no source claimed"
        )
        failures.append(
            Failure(
                "unclaimed",
                f"{table_ref(table=table)} {why}. Query: {sql}",
            )
        )
    for source, reply in zip(
        pack.waiting_sources, replies[len(queries) :], strict=True
    ):
        rows = rows_of(reply=reply)
        number = first_number(rows=rows) if rows else None
        if number:
            failures.append(
                Failure(
                    "unclaimed",
                    f"{table_ref(table=source)} holds {int(number)} rows, so {source} no longer waits on {pack.waiting_reason}. "
                    f"Remove it from {VERIFY_FILE} waiting",
                    retry=False,
                )
            )
    held = ", ".join(f"{table} {rows}" for table, rows in counts.items())
    if pack.waiting_sources:
        held += f"; {', '.join(pack.waiting_sources)} rows left out of {DEFAULT_TABLE}, waiting on {pack.waiting_reason}"
    return Result("unclaimed", held, tuple(failures))


def check_sources(*, pack: Pack, stack: Stack) -> Result:
    """Every pack source is deployed and in the state its file declares."""
    (reply,) = stack.calls([Request("GET", f"{ENGINE_API}/sources?page_size=500")])
    items = reply.body.get("items") if isinstance(reply.body, dict) else None
    if reply.status != 200 or not isinstance(items, list):
        return Result(
            "sources",
            "",
            (
                Failure(
                    "sources", f"GET /api/v1/sources answered {fault_of(reply=reply)}"
                ),
            ),
        )
    held = {item.get("name"): item for item in items if isinstance(item, dict)}
    failures = []
    for source in pack.sources:
        item = held.get(source)
        want = pack.states[source]
        if item is None:
            failures.append(
                Failure("sources", f"{source} is not in GET /api/v1/sources")
            )
            continue
        if item.get("state") != want:
            failures.append(
                Failure(
                    "sources",
                    f"{source} is {item.get('state')!r}, its file declares {want!r}",
                )
            )
        if not item.get("deployed_version") or item.get("deployed_version") != item.get(
            "current"
        ):
            failures.append(
                Failure(
                    "sources",
                    f"{source} is deployed at {item.get('deployed_version')!r} where its current version is {item.get('current')!r}",
                )
            )
    return Result("sources", f"{len(pack.sources)} sources deployed", tuple(failures))


def check_hunts(*, pack: Pack, stack: Stack) -> Result:
    """Each rule of each hunt wrote detections, and each hunt stayed under its ceiling."""
    if not pack.hunts:
        return Result("hunts", f"{pack.name} ships no hunt", skipped=True)
    names = ", ".join(f"'{name}'" for name in pack.hunts)
    sql = (
        f"SELECT hunt_name, rule_id, count() FROM {table_ref(table=DETECTION_TABLE)} "
        f"WHERE hunt_name IN ({names}) GROUP BY hunt_name, rule_id"
    )
    (reply,) = run_sql(queries=[sql], stack=stack)
    rows = rows_of(reply=reply)
    if rows is None:
        return Result(
            "hunts",
            "",
            (
                Failure(
                    "hunts",
                    f"{table_ref(table=DETECTION_TABLE)} could not be read ({fault_of(reply=reply)}). Query: {sql}",
                ),
            ),
        )
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        hunt, rule, number = list(row.values())[:3]
        seen[(str(hunt), str(rule))] = int(number)
    failures = []
    held = []
    for hunt, rules in pack.hunts.items():
        total = sum(number for (name, _), number in seen.items() if name == hunt)
        for rule in rules:
            if not seen.get((hunt, rule)):
                failures.append(
                    Failure(
                        "hunts",
                        f"hunt {hunt}: rule {rule} wrote no detection yet. Query: {sql}",
                    )
                )
        ceiling = pack.hunt_limits.get(hunt, {}).get("max_alerts")
        if not isinstance(ceiling, int):
            failures.append(
                Failure(
                    "hunts",
                    f"{VERIFY_FILE} names no max_alerts for hunt {hunt}",
                    retry=False,
                )
            )
        elif total > ceiling:
            failures.append(
                Failure(
                    "hunts",
                    f"hunt {hunt} wrote {total} detections, over its ceiling of {ceiling}. Query: {sql}",
                    retry=False,
                )
            )
        held.append(f"{hunt} {total}")
    return Result("hunts", ", ".join(held), tuple(failures))


def console_requests() -> list[Request]:
    """The reads that list what the console holds: its sources, dashboards and saved searches."""
    return [
        Request("GET", f"{HYPERDX_API}/sources"),
        Request("GET", f"{HYPERDX_API}/dashboards"),
        Request("GET", f"{HYPERDX_API}/saved-search"),
    ]


def read_console(
    *, stack: Stack
) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]] | str:
    """The installed dashboards and searches by name and each source's table by id, or why they cannot be read."""
    replies = stack.calls(console_requests())
    for reply, what in zip(
        replies, ("sources", "dashboards", "saved-search"), strict=True
    ):
        if reply.status != 200 or not isinstance(reply.body, list):
            return f"GET {HYPERDX_API}/{what} answered {fault_of(reply=reply)}"
    sources = {
        str(item.get("id")): str((item.get("from") or {}).get("tableName"))
        for item in replies[0].body
        if isinstance(item, dict)
    }
    dashboards = {
        str(item.get("name")): item
        for item in replies[1].body
        if isinstance(item, dict)
    }
    searches = {
        str(item.get("name")): item
        for item in replies[2].body
        if isinstance(item, dict)
    }
    return dashboards, searches, sources


def check_console(*, pack: Pack, stack: Stack) -> Result:
    """Every dashboard and saved search the pack names is installed and its queries return rows."""
    if not pack.dashboards and not pack.searches:
        return Result(
            "console", f"{pack.name} installs no dashboard or search", skipped=True
        )
    read = read_console(stack=stack)
    if isinstance(read, str):
        return Result("console", "", (Failure("console", read),))
    dashboards, searches, tables = read
    failures: list[Failure] = []
    plan: list[tuple[str, str, str, str]] = []
    for name in pack.dashboards:
        found = dashboards.get(name)
        if found is None:
            failures.append(
                Failure("console", f"dashboard {name} is not installed in dfe-hyperdx")
            )
            continue
        for tile in found.get("tiles") or []:
            config = tile.get("config") or {}
            sql = config.get("sqlTemplate")
            if sql:
                plan.append(
                    (
                        f"dashboard {name}, tile {tile.get('id')}",
                        f"{name}/{tile.get('id')}",
                        str(config.get("displayType")),
                        expand_macros(sql=sql),
                    )
                )
    for name in pack.searches:
        found = searches.get(name)
        if found is None:
            failures.append(
                Failure(
                    "console", f"saved search {name} is not installed in dfe-hyperdx"
                )
            )
            continue
        table = tables.get(str(found.get("source")))
        if not table:
            failures.append(
                Failure(
                    "console",
                    f"saved search {name} reads source {found.get('source')!r}, which dfe-hyperdx does not hold",
                )
            )
            continue
        plan.append(
            (
                f"saved search {name}",
                name,
                "search",
                search_query(search=found, table=table),
            )
        )
    replies = run_sql(queries=[sql for *_, sql in plan], stack=stack)
    for (label, key, kind, sql), reply in zip(plan, replies, strict=True):
        rows = rows_of(reply=reply)
        if rows is None:
            failures.append(
                Failure(
                    "console",
                    f"{label} did not run ({fault_of(reply=reply)}). Query: {sql}",
                )
            )
        elif key in pack.zero_ok:
            continue
        elif not rows or (kind in ("search", "number") and not first_number(rows=rows)):
            failures.append(
                Failure(
                    "console",
                    f"{label} returns no rows or reads 0, and {key} is not in {VERIFY_FILE} zero_ok. Query: {sql}",
                )
            )
    return Result(
        "console",
        f"{len(pack.dashboards)} dashboards and {len(pack.searches)} searches, {len(plan)} queries",
        tuple(failures),
    )


def check_search(*, pack: Pack, stack: Stack) -> Result:
    """A saved search's query runs through the engine's query API as the console login, and returns rows unless verify.json lets it return none."""
    names = list(pack.searches)
    if not names:
        return Result("search", f"{pack.name} installs no saved search", skipped=True)
    fault = getattr(stack, "login_fault", "")
    if fault:
        return Result("search", "", (Failure("search", fault),))
    read = read_console(stack=stack)
    if isinstance(read, str):
        return Result("search", "", (Failure("search", read),))
    _, searches, tables = read
    failures: list[Failure] = []
    plan: list[tuple[str, str]] = []
    for name in names:
        found = searches.get(name)
        table = tables.get(str(found.get("source"))) if found else None
        if not found or not table:
            failures.append(
                Failure(
                    "search",
                    f"saved search {name} is not installed or reads no table",
                )
            )
            continue
        plan.append((name, search_query(search=found, table=table)))
    replies = run_sql(queries=[sql for _, sql in plan], stack=stack, identity="console")
    for (name, sql), reply in zip(plan, replies, strict=True):
        rows = rows_of(reply=reply)
        if rows is None:
            why = f"got {fault_of(reply=reply)}"
        elif name not in pack.zero_ok and not first_number(rows=rows):
            why = "returned no rows"
        else:
            continue
        failures.append(
            Failure("search", f"{name} as the console login {why}. Query: {sql}")
        )
    may_be_empty = [name for name, _ in plan if name in pack.zero_ok]
    held = f"{len(plan) - len(may_be_empty)} saved searches returned rows as the console login"
    if may_be_empty:
        held += f", and {len(may_be_empty)} that may return none ran without an error"
    return Result("search", held, tuple(failures))


def check_fetchers(*, pack: Pack, stack: Stack) -> Result:
    """Each fetcher source whose polls the pack replays holds rows, and the others are skipped with their reason."""
    if not pack.fetchers:
        return Result("fetchers", f"{pack.name} has no fetcher source", skipped=True)
    waiting = [source for source in pack.fetchers if source in pack.waiting_sources]
    unreplayed = [
        source
        for source in pack.fetchers
        if source not in pack.polled and source not in waiting
    ]
    notes = []
    if unreplayed:
        notes.append(
            f"skipped {', '.join(unreplayed)}: no captured poll in events/<source>/demo.log, so they wait on their upstream"
        )
    if waiting:
        notes.append(f"skipped {', '.join(waiting)}: waiting on {pack.waiting_reason}")
    note = "; ".join(notes)
    if not pack.polled:
        return Result("fetchers", note, skipped=True)
    counts = count_tables(stack=stack, tables=pack.polled)
    failures = tuple(
        Failure(
            "fetchers",
            f"{table_ref(table=table)} holds {'an unreadable count' if rows is None else f'{rows} rows'} after its replayed polls. Query: SELECT count() FROM {table_ref(table=table)}",
        )
        for table, rows in counts.items()
        if not rows
    )
    held = ", ".join(f"{table} {rows}" for table, rows in counts.items())
    return Result("fetchers", f"{held}{'; ' + note if note else ''}", failures)


def check_dlq(*, pack: Pack, stack: Stack) -> Result:
    """dfe.dlq and each dead-letter topic exist and hold nothing, and a record names its source and reason."""
    sql = (
        f"SELECT service, ifNull(_source, ''), destination, reason, count() "
        f"FROM {table_ref(table=DLQ_TABLE)} GROUP BY service, _source, destination, reason"
    )
    (reply,) = run_sql(queries=[sql], stack=stack)
    rows = rows_of(reply=reply)
    failures: list[Failure] = []
    if rows is None:
        failures.append(
            Failure(
                "dlq",
                f"{table_ref(table=DLQ_TABLE)} could not be read ({fault_of(reply=reply)}). Query: {sql}",
            )
        )
    for row in rows or []:
        service, source, destination, reason, number = list(row.values())[:5]
        failures.append(
            Failure(
                "dlq",
                f"{number} records dead-lettered by {service} for source {source!r} heading to {destination!r}: {str(reason)[:300]}. Query: {sql}",
            )
        )
    marks: dict[str, int | None] = {}
    if KAFKA_SERVICE in stack.services():
        marks = stack.topic_marks(DLQ_TOPICS)
        for topic, mark in marks.items():
            if mark is None:
                failures.append(
                    Failure(
                        "dlq",
                        f"topic {topic} is missing or unreadable, and a record sent to it is held and back-pressures its source. The engine creates every DLQ topic at startup: rpk topic describe {topic} -p",
                    )
                )
            elif mark:
                failures.append(
                    Failure(
                        "dlq",
                        f"topic {topic} holds {mark} records: rpk topic consume {topic} -n 3",
                    )
                )
    seen = "table dfe.dlq" + (
        f" and {len(marks)} topics" if marks else ", no broker in this stack"
    )
    return Result("dlq", f"{seen} hold nothing", tuple(failures))


def verify(
    *,
    only: Sequence[str] = CHECKS,
    pack: Pack,
    stack: Stack,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wait_scale: float = 1.0,
) -> list[Result]:
    """Run each wanted check in order, the slow ones until they hold or their wait runs out, and return every result.

    `wait_scale` multiplies every wait, 0 for a single try.
    """
    waits = {
        "landed": LAND_TIMEOUT_SECONDS,
        "hunts": HUNT_TIMEOUT_SECONDS,
        "console": CONSOLE_TIMEOUT_SECONDS,
        "fetchers": FETCH_TIMEOUT_SECONDS,
    }
    functions = {
        "landed": check_landed,
        "parsed": check_parsed,
        "unclaimed": check_unclaimed,
        "sources": check_sources,
        "hunts": check_hunts,
        "console": check_console,
        "search": check_search,
        "fetchers": check_fetchers,
        "dlq": check_dlq,
    }
    results = []
    for name in CHECKS:
        if name not in only:
            continue
        probe = lambda check=functions[name]: check(pack=pack, stack=stack)  # noqa: E731
        if name in waits:
            results.append(
                wait_for(
                    probe=probe,
                    timeout=waits[name] * wait_scale,
                    clock=clock,
                    sleep=sleep,
                )
            )
        else:
            results.append(probe())
    return results


def load_pack(*, name: str, root: Path | None = None) -> Pack:
    """Read the pack's files, refusing a pack that is missing one or holds a file this cannot read."""
    short = name.removeprefix("demo-") if name != "demo" else ""
    root = root or demo_pack_dir(pack=short)
    manifest = _json_file(path=root / "demo.json")
    verify_data = _json_file(path=root / VERIFY_FILE)
    sources = _names(value=manifest.get("sources"), what="demo.json sources")
    console = manifest.get("console") or {}
    texts = {source: _source_text(root=root, source=source) for source in sources}
    intake = next(
        (source for source, text in texts.items() if AGENTLESS_LINE.search(text)), None
    )
    fetchers = [source for source, text in texts.items() if FETCHER_LINE.search(text)]
    waiting = verify_data.get("waiting") or {}
    if not isinstance(waiting, dict):
        raise VerifyError(f"{VERIFY_FILE} waiting needs an object")
    waiting_sources = _names(
        value=waiting.get("sources", []), what=f"{VERIFY_FILE} waiting sources"
    )
    captured = [
        source
        for source in sources
        if (root / "events" / source / "demo.log").is_file()
        and source not in waiting_sources
    ]
    hunts = {
        hunt: _hunt_rules(path=root / "hunts" / f"{hunt}.yaml")
        for hunt in _names(value=manifest.get("hunts") or [], what="demo.json hunts")
    }
    return Pack(
        name=name,
        sources=sources,
        states={
            source: (found.group(1) if (found := STATE_LINE.search(text)) else "active")
            for source, text in texts.items()
        },
        replayed=[
            source for source in captured if source not in fetchers and source != intake
        ],
        polled=[source for source in captured if source in fetchers],
        fetchers=fetchers,
        intake=intake,
        dashboards=_names(value=console.get("dashboards"), what="demo.json dashboards"),
        searches=_names(value=console.get("searches"), what="demo.json searches"),
        hunts=hunts,
        parsed=_objects(value=verify_data.get("parsed"), what=f"{VERIFY_FILE} parsed"),
        hunt_limits=_objects(
            value=verify_data.get("hunts"), what=f"{VERIFY_FILE} hunts"
        ),
        waiting_sources=waiting_sources,
        waiting_reason=str(waiting.get("reason", "")),
        zero_ok=set(
            _names(
                value=verify_data.get("zero_ok") or [], what=f"{VERIFY_FILE} zero_ok"
            )
        ),
    )


def _json_file(*, path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerifyError(f"{str(path)!r} is not readable JSON: {error}") from error
    if not isinstance(loaded, dict):
        raise VerifyError(f"{str(path)!r} needs a JSON object")
    return loaded


def _names(*, value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise VerifyError(f"{what} needs a list of names")
    return list(value)


def _objects(*, value: object, what: str) -> dict[str, dict]:
    if not isinstance(value, dict) or not all(
        isinstance(item, dict) for item in value.values()
    ):
        raise VerifyError(f"{what} needs an object of objects")
    return dict(value)


def _source_text(*, root: Path, source: str) -> str:
    path = root / "sources" / f"{source}.yaml"
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise VerifyError(f"{str(path)!r} is not readable: {error}") from error


def _hunt_rules(*, path: Path) -> list[str]:
    """The rule names under a hunt file's `rules:` list."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise VerifyError(f"{str(path)!r} is not readable: {error}") from error
    rules: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("rules:"):
            inside = True
        elif inside and (found := RULE_LINE.match(line)):
            rules.append(found.group(1))
        elif inside and line.strip() and not line.startswith("#"):
            break
    if not rules:
        raise VerifyError(f"{str(path)!r} lists no rules")
    return rules


def _report(*, results: Sequence[Result]) -> int:
    failed = 0
    for result in results:
        if result.skipped:
            _print(msg=f"SKIP {result.check}: {result.detail}")
        elif result.failures:
            failed += 1
            for failure in result.failures:
                _print(msg=f"FAIL {failure.check}: {failure.detail}")
        else:
            _print(msg=f"PASS {result.check}: {result.detail}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pack", required=True, help="the pack: demo, or demo-<name>")
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="the pack's published compose file, which is up",
    )
    parser.add_argument(
        "--context", default="", help="the docker context the stack runs in"
    )
    parser.add_argument(
        "--wait-scale",
        default=1.0,
        type=float,
        help="multiply every wait, 0 to try each check once",
    )
    parser.add_argument(
        "--only",
        default=",".join(CHECKS),
        help=f"comma-separated checks to run, from {', '.join(CHECKS)}",
    )
    args = parser.parse_args()
    only = [name for name in args.only.split(",") if name]
    unknown = sorted(set(only) - set(CHECKS))
    if unknown:
        parser.error(f"no check {unknown}, the checks are {', '.join(CHECKS)}")
    try:
        pack = load_pack(name=args.pack)
        stack = Compose(file=args.file.resolve(), context=args.context)
        if "search" in only:
            stack = stack.with_minted_login()
        results = verify(only=only, pack=pack, stack=stack, wait_scale=args.wait_scale)
    except (VerifyError, OSError, subprocess.TimeoutExpired) as error:
        _print(msg=f"FAIL {error}")
        return 1
    return _report(results=results)


if __name__ == "__main__":
    raise SystemExit(main())
