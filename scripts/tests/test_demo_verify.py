#  Project:      dfe-docker
#  File:         tests/test_demo_verify.py
#  Purpose:      Assert each demo_verify check passes a pack that worked and fails one that did not, naming the table, the count and the query
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The pack checker over a stack that answers from a table, plus every real pack's own files.

The stack here is a function from a request to a reply, so each check runs its real queries against canned answers: nothing in demo_verify is replaced, and a check that built the wrong SQL finds no answer for it.
"""

import re
from collections.abc import Callable, Sequence

import pytest

import demo_verify as dv
import publish_compose
from _common import demo_pack_names


class Answers:
    """A stack whose replies come from `answer`, keyed on the request, and whose broker holds `marks`."""

    def __init__(
        self,
        answer: Callable[[dv.Request], dv.Reply],
        *,
        marks: dict[str, int | None] | None = None,
        services: Sequence[str] = ("dfe-engine", "kafka-redpanda"),
    ) -> None:
        self.answer = answer
        self.marks = marks if marks is not None else dict.fromkeys(dv.DLQ_TOPICS, 0)
        self.listed = list(services)
        self.sent: list[dv.Request] = []

    def calls(self, requests: Sequence[dv.Request]) -> list[dv.Reply]:
        self.sent.extend(requests)
        return [self.answer(request) for request in requests]

    def services(self) -> list[str]:
        return self.listed

    def topic_marks(self, topics: Sequence[str]) -> dict[str, int | None]:
        return {topic: self.marks.get(topic) for topic in topics}


def rows(*cells: object) -> dv.Reply:
    return dv.Reply(200, {"rows": [{f"c{i}": cell for i, cell in enumerate(cells)}]})


def table(*lines: Sequence[object]) -> dv.Reply:
    return dv.Reply(
        200,
        {"rows": [{f"c{i}": cell for i, cell in enumerate(line)} for line in lines]},
    )


NONE = dv.Reply(200, {"rows": []})
REFUSED = dv.Reply(500, {"detail": "The query failed"})


def pack(**changes: object) -> dv.Pack:
    base: dict = {
        "name": "demo-test",
        "sources": ["okta", "wiki", "demo-syslog"],
        "states": {"okta": "active", "wiki": "active", "demo-syslog": "active"},
        "replayed": ["okta"],
        "polled": [],
        "fetchers": [],
        "intake": "demo-syslog",
        "dashboards": ["demo-t"],
        "searches": ["demo-t-live"],
        "hunts": {"demo-hunt": ["demo-rule"]},
        "parsed": {"okta": {"fields": ["event_action", "_json.a.b"], "min_ratio": 0.9}},
        "hunt_limits": {"demo-hunt": {"max_alerts": 10}},
        "zero_ok": set(),
    }
    base.update(changes)
    return dv.Pack(**base)


def only(check: str, **kwargs: object) -> dv.Result:
    results = dv.verify(only=[check], **kwargs, sleep=lambda _: None)
    assert [result.check for result in results] == [check]
    return results[0]


def clock_of(step: float) -> Callable[[], float]:
    now = [0.0]

    def tick() -> float:
        now[0] += step
        return now[0]

    return tick


def landing(counts: dict[str, int]) -> Callable[[dv.Request], dv.Reply]:
    def answer(request: dv.Request) -> dv.Reply:
        query = request.payload["query"]
        for name, count in counts.items():
            if f"`{name}`" in query:
                return rows(count)
        return rows(0)

    return answer


@pytest.mark.parametrize(
    ("name", "fragment"),
    [
        ("event_action", "toString(event_action) NOT IN"),
        ("_json.a.b", "toString(_json.a.b) NOT IN"),
    ],
)
def test_present_reads_a_column_or_a_json_path(name: str, fragment: str) -> None:
    assert fragment in dv.present(name=name)


@pytest.mark.parametrize(
    "name", ["a; SELECT 1", "a b", "", "1a", "a..b", "a)", "`a`", "a-b"]
)
def test_a_field_that_could_change_the_query_is_refused(name: str) -> None:
    with pytest.raises(dv.VerifyError):
        dv.present(name=name)


@pytest.mark.parametrize("table_name", ["a`b", "a b", "a;b", "", "a.b"])
def test_a_table_name_that_could_change_the_query_is_refused(table_name: str) -> None:
    with pytest.raises(dv.VerifyError):
        dv.table_ref(table=table_name)


def test_coverage_counts_rows_errors_then_each_field_in_order() -> None:
    sql = dv.coverage_query(fields=["a", "b"], table="cisco-asa")
    assert sql.startswith("SELECT count(), countIf((JSONHas(")
    assert sql.index("toString(a)") < sql.index("toString(b)")
    assert sql.endswith("FROM dfe.`cisco-asa`")


def test_time_macros_become_a_window_an_interval_and_a_step() -> None:
    sql = dv.expand_macros(
        sql="SELECT $__timeInterval(t), count() * 60 / $__interval_s FROM x WHERE $__timeFilter(t)"
    )
    assert "$__" not in sql
    assert "toStartOfInterval(t, INTERVAL 60 SECOND)" in sql
    assert "t >= now() - INTERVAL 3600 SECOND" in sql
    assert "/ 60 " in sql


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (
            "region\nPARTITION LEADER EPOCH REPLICAS LOG-START-OFFSET HIGH-WATERMARK\n0 0 1 [0] 0 7\n1 0 1 [0] 0 5\n",
            12,
        ),
        (
            "PARTITION LEADER EPOCH REPLICAS LOG-START-OFFSET HIGH-WATERMARK\n0 0 1 [0] 0 0\n",
            0,
        ),
        ("PARTITION LEADER\n0 0\n", None),
        ("", None),
        (
            "PARTITION LEADER EPOCH REPLICAS LOG-START-OFFSET HIGH-WATERMARK\n0 0 1 [0] 0 x\n",
            None,
        ),
    ],
)
def test_a_topics_watermark_is_summed_or_unknown(text: str, want: int | None) -> None:
    assert (
        dv.watermark_total(
            text=text.split("\n", 1)[1] if text.startswith("region") else text
        )
        == want
    )


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Console login: admin / s3cret\n", ("admin", "s3cret")),
        ("noise\nConsole login: a.b@c.io / pw\nmore", ("a.b@c.io", "pw")),
        ("Console login: only\n", None),
        ("", None),
    ],
)
def test_the_minted_login_is_read_from_its_line(text: str, want: tuple | None) -> None:
    assert dv.parse_login(text=text) == want


def test_landed_passes_a_pack_whose_sources_hold_rows() -> None:
    result = only("landed", pack=pack(), stack=Answers(landing({"okta": 5})))
    assert not result.failures


def test_landed_fails_a_source_with_no_rows_and_names_its_query() -> None:
    result = only(
        "landed",
        pack=pack(),
        stack=Answers(landing({"okta": 0})),
        clock=clock_of(1000),
    )
    assert [failure.check for failure in result.failures] == ["landed"]
    assert "dfe.okta holds 0 rows" in result.failures[0].detail
    assert "SELECT count() FROM dfe.`okta`" in result.failures[0].detail


def coverage(
    total: int, errored: int, *fields: int
) -> Callable[[dv.Request], dv.Reply]:
    return lambda request: rows(total, errored, *fields)


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        (coverage(100, 0, 100, 100), None),
        (coverage(100, 0, 90, 95), None),
        (
            coverage(100, 3, 100, 100),
            "3 of 100 rows carry error.message or a pipeline_error tag",
        ),
        (coverage(100, 0, 100, 89), "_json.a.b is set on 89 of 100 rows (89%)"),
        (coverage(100, 0, 0, 0), "event_action is set on 0 of 100 rows"),
        (coverage(0, 0, 0, 0), "holds 0 rows"),
        (lambda request: REFUSED, "could not be read"),
    ],
)
def test_parsed_judges_errors_and_each_expected_field(
    answer: Callable[[dv.Request], dv.Reply], needle: str | None
) -> None:
    result = only("parsed", pack=pack(), stack=Answers(answer))
    if needle is None:
        assert not result.failures
    else:
        assert any(needle in failure.detail for failure in result.failures), result


def test_parsed_names_the_query_that_finds_the_errored_rows() -> None:
    result = only("parsed", pack=pack(), stack=Answers(coverage(10, 2, 10, 10)))
    assert (
        "WHERE (JSONHas(toJSONString(_json), 'error', 'message')"
        in result.failures[0].detail
    )


@pytest.mark.parametrize(
    ("counts", "failed"),
    [
        ({"demo-syslog": 0, "main": 0}, []),
        ({"demo-syslog": 4, "main": 0}, ["demo-syslog"]),
        ({"demo-syslog": 0, "main": 2}, ["main"]),
        ({"demo-syslog": 1, "main": 1}, ["demo-syslog", "main"]),
    ],
)
def test_unclaimed_fails_a_table_no_source_should_have_filled(
    counts: dict[str, int], failed: list[str]
) -> None:
    result = only("unclaimed", pack=pack(), stack=Answers(landing(counts)))
    assert [f.detail.split("`")[1] for f in result.failures] == failed


def test_unclaimed_reads_only_the_default_table_for_a_pack_with_no_intake() -> None:
    stack = Answers(landing({"main": 0}))
    assert not only("unclaimed", pack=pack(intake=None), stack=stack).failures
    assert len(stack.sent) == 1


def listing(*items: dict) -> Callable[[dv.Request], dv.Reply]:
    return lambda request: dv.Reply(200, {"items": list(items)})


def source(name: str, *, state: str = "active", deployed: str | None = "1.0.0") -> dict:
    return {
        "name": name,
        "state": state,
        "current": "1.0.0",
        "deployed_version": deployed,
    }


@pytest.mark.parametrize(
    ("items", "needle"),
    [
        ([source("okta"), source("wiki"), source("demo-syslog")], None),
        ([source("okta"), source("demo-syslog")], "wiki is not in GET /api/v1/sources"),
        (
            [source("okta"), source("wiki", state="dormant"), source("demo-syslog")],
            "wiki is 'dormant', its file declares 'active'",
        ),
        (
            [source("okta"), source("wiki", deployed=None), source("demo-syslog")],
            "wiki is deployed at None",
        ),
        (
            [source("okta"), source("wiki", deployed="0.9.0"), source("demo-syslog")],
            "deployed at '0.9.0'",
        ),
    ],
)
def test_sources_must_be_deployed_in_the_state_their_file_declares(
    items: list[dict], needle: str | None
) -> None:
    result = only("sources", pack=pack(), stack=Answers(listing(*items)))
    if needle is None:
        assert not result.failures
    else:
        assert any(needle in failure.detail for failure in result.failures)


def test_a_dormant_source_passes_where_its_file_declares_dormant() -> None:
    items = [source("okta"), source("wiki"), source("demo-syslog", state="dormant")]
    states = {"okta": "active", "wiki": "active", "demo-syslog": "dormant"}
    assert not only(
        "sources", pack=pack(states=states), stack=Answers(listing(*items))
    ).failures


def test_sources_fails_when_the_engine_does_not_answer() -> None:
    result = only("sources", pack=pack(), stack=Answers(lambda request: REFUSED))
    assert "GET /api/v1/sources answered HTTP 500" in result.failures[0].detail


@pytest.mark.parametrize(
    ("detections", "limits", "needle", "retry"),
    [
        (
            [("demo-hunt", "demo-rule", 3)],
            {"demo-hunt": {"max_alerts": 10}},
            None,
            None,
        ),
        (
            [("demo-hunt", "demo-rule", 10)],
            {"demo-hunt": {"max_alerts": 10}},
            None,
            None,
        ),
        (
            [],
            {"demo-hunt": {"max_alerts": 10}},
            "rule demo-rule wrote no detection yet",
            True,
        ),
        (
            [("demo-hunt", "demo-rule", 11)],
            {"demo-hunt": {"max_alerts": 10}},
            "wrote 11 detections, over its ceiling of 10",
            False,
        ),
        (
            [("demo-hunt", "demo-rule", 3)],
            {},
            "names no max_alerts for hunt demo-hunt",
            False,
        ),
        (
            [("demo-hunt", "other-rule", 3)],
            {"demo-hunt": {"max_alerts": 10}},
            "rule demo-rule wrote no detection yet",
            True,
        ),
    ],
)
def test_hunts_must_fire_every_rule_and_stay_under_the_ceiling(
    detections: list[tuple], limits: dict, needle: str | None, retry: bool | None
) -> None:
    stack = Answers(lambda request: table(*detections) if detections else NONE)
    result = only(
        "hunts", pack=pack(hunt_limits=limits), stack=stack, clock=clock_of(1000)
    )
    if needle is None:
        assert not result.failures
    else:
        assert any(needle in failure.detail for failure in result.failures)
        assert all(failure.retry is retry for failure in result.failures)


def test_a_hunt_over_its_ceiling_is_not_waited_on() -> None:
    sleeps: list[float] = []
    stack = Answers(lambda request: table(("demo-hunt", "demo-rule", 99)))
    dv.verify(only=["hunts"], pack=pack(), stack=stack, sleep=sleeps.append)
    assert sleeps == []


def console(
    *, dashboards: list[dict], searches: list[dict]
) -> Callable[[dv.Request, Callable], dv.Reply]:
    def answer(request: dv.Request, run: Callable[[str], dv.Reply]) -> dv.Reply:
        if request.url.endswith("/sources"):
            return dv.Reply(200, [{"id": "s1", "from": {"tableName": "okta"}}])
        if request.url.endswith("/dashboards"):
            return dv.Reply(200, dashboards)
        if request.url.endswith("/saved-search"):
            return dv.Reply(200, searches)
        return run(request.payload["query"])

    return answer


TILE = {
    "id": "t1",
    "config": {
        "displayType": "table",
        "sqlTemplate": "SELECT 1 FROM dfe.okta WHERE $__timeFilter(t)",
    },
}
NUMBER = {
    "id": "n1",
    "config": {"displayType": "number", "sqlTemplate": "SELECT count() FROM dfe.okta"},
}
SEARCH = {"name": "demo-t-live", "source": "s1", "select": "a", "where": "b = 1"}


def run_console(
    dashboards: list[dict],
    searches: list[dict],
    run: Callable[[str], dv.Reply],
    **changes: object,
) -> dv.Result:
    answer = console(dashboards=dashboards, searches=searches)
    stack = Answers(lambda request: answer(request, run))
    return only("console", pack=pack(**changes), stack=stack, clock=clock_of(1000))


@pytest.mark.parametrize(
    ("dashboards", "searches", "run", "changes", "needle"),
    [
        (
            [{"name": "demo-t", "tiles": [TILE]}],
            [SEARCH],
            lambda sql: rows(4),
            {},
            None,
        ),
        ([], [SEARCH], lambda sql: rows(4), {}, "dashboard demo-t is not installed"),
        (
            [{"name": "demo-t", "tiles": [TILE]}],
            [],
            lambda sql: rows(4),
            {},
            "saved search demo-t-live is not installed",
        ),
        (
            [{"name": "demo-t", "tiles": [TILE]}],
            [SEARCH],
            lambda sql: NONE,
            {},
            "dashboard demo-t, tile t1 returns no rows or reads 0",
        ),
        (
            [{"name": "demo-t", "tiles": [TILE]}],
            [SEARCH],
            lambda sql: rows(0) if "LIMIT 100" in sql else rows(1),
            {},
            "saved search demo-t-live returns no rows or reads 0",
        ),
        (
            [{"name": "demo-t", "tiles": [TILE]}],
            [SEARCH],
            lambda sql: REFUSED,
            {},
            "did not run (HTTP 500",
        ),
        (
            [{"name": "demo-t", "tiles": [NUMBER]}],
            [SEARCH],
            lambda sql: rows(0) if "count()" in sql and "LIMIT" not in sql else rows(1),
            {},
            "demo-t/n1 is not in verify.json zero_ok",
        ),
        (
            [{"name": "demo-t", "tiles": [NUMBER]}],
            [SEARCH],
            lambda sql: rows(0) if "count()" in sql and "LIMIT" not in sql else rows(1),
            {"zero_ok": {"demo-t/n1"}},
            None,
        ),
        (
            [{"name": "demo-t", "tiles": [TILE]}],
            [SEARCH],
            lambda sql: NONE if "LIMIT" not in sql else rows(0),
            {"zero_ok": {"demo-t/t1", "demo-t-live"}},
            None,
        ),
    ],
)
def test_console_runs_every_tile_and_search(
    dashboards, searches, run, changes, needle
) -> None:
    result = run_console(dashboards, searches, run, **changes)
    if needle is None:
        assert not result.failures, result
    else:
        assert any(needle in failure.detail for failure in result.failures), result


def test_console_refuses_a_search_that_reads_a_source_the_console_lacks() -> None:
    result = run_console(
        [{"name": "demo-t", "tiles": []}],
        [{**SEARCH, "source": "gone"}],
        lambda sql: rows(1),
    )
    assert (
        "reads source 'gone', which dfe-hyperdx does not hold"
        in result.failures[0].detail
    )


def test_search_runs_the_first_saved_search_as_the_console_login() -> None:
    answer = console(dashboards=[], searches=[SEARCH])
    stack = Answers(lambda request: answer(request, lambda sql: rows(3)))
    assert not only("search", pack=pack(), stack=stack).failures
    assert stack.sent[-1].identity == "console"


@pytest.mark.parametrize(
    ("run", "needle"),
    [
        (
            lambda sql: dv.Reply(403, {"detail": "password change required"}),
            "as the console login got HTTP 403",
        ),
        (lambda sql: rows(0), "as the console login returned no rows"),
    ],
)
def test_search_fails_where_the_console_login_cannot_read_rows(run, needle) -> None:
    answer = console(dashboards=[], searches=[SEARCH])
    result = only(
        "search", pack=pack(), stack=Answers(lambda request: answer(request, run))
    )
    assert needle in result.failures[0].detail


def test_fetchers_skip_a_pack_with_none_and_say_why_for_an_unreplayed_one() -> None:
    assert only("fetchers", pack=pack(), stack=Answers(landing({}))).skipped
    unreplayed = only(
        "fetchers", pack=pack(fetchers=["wiki"]), stack=Answers(landing({}))
    )
    assert unreplayed.skipped
    assert "skipped wiki: no captured poll" in unreplayed.detail


@pytest.mark.parametrize(
    ("count", "failed"),
    [(7, False), (0, True)],
)
def test_fetchers_need_rows_where_the_polls_are_replayed(
    count: int, failed: bool
) -> None:
    result = only(
        "fetchers",
        pack=pack(fetchers=["wiki"], polled=["wiki"]),
        stack=Answers(landing({"wiki": count})),
        clock=clock_of(1000),
    )
    assert bool(result.failures) is failed
    if failed:
        assert (
            "dfe.`wiki` holds 0 rows after its replayed polls"
            in result.failures[0].detail
        )


@pytest.mark.parametrize(
    ("dead", "marks", "services", "needles"),
    [
        (NONE, None, None, []),
        (
            table(("dfe-loader", "okta", "dfe.okta", "type mismatch", 3)),
            None,
            None,
            [
                "3 records dead-lettered by dfe-loader for source 'okta'",
                "type mismatch",
            ],
        ),
        (NONE, {"dfe_loader_dlq": 2}, None, ["topic dfe_loader_dlq holds 2 records"]),
        (
            NONE,
            {"dfe_fetcher_dlq": None},
            None,
            ["topic dfe_fetcher_dlq is missing or unreadable"],
        ),
        (NONE, {"dfe_loader_dlq": 9}, ["dfe-engine"], []),
        (REFUSED, None, None, ["could not be read"]),
    ],
)
def test_dlq_fails_on_any_record_in_the_table_or_a_topic(
    dead, marks, services, needles
) -> None:
    stack = Answers(
        lambda request: dead,
        marks={**dict.fromkeys(dv.DLQ_TOPICS, 0), **(marks or {})},
        services=services or ["dfe-engine", "kafka-redpanda"],
    )
    result = only("dlq", pack=pack(), stack=stack)
    for needle in needles:
        assert any(needle in failure.detail for failure in result.failures), result
    assert bool(result.failures) is bool(needles)


def test_verify_runs_the_checks_in_order_and_only_the_wanted_ones() -> None:
    stack = Answers(
        lambda request: (
            rows(1)
            if "count()" in request.payload["query"]
            and "GROUP BY" not in request.payload["query"]
            else NONE
        )
    )
    results = dv.verify(only=["dlq", "landed"], pack=pack(), stack=stack)
    assert [result.check for result in results] == ["landed", "dlq"]


def test_a_check_that_never_holds_is_asked_again_until_its_wait_ends() -> None:
    sleeps: list[float] = []
    result = dv.wait_for(
        probe=lambda: dv.Result("x", "", (dv.Failure("x", "not yet"),)),
        timeout=20,
        clock=clock_of(5),
        sleep=sleeps.append,
    )
    assert result.failures
    assert len(sleeps) == 3


@pytest.mark.parametrize("name", demo_pack_names())
def test_a_real_pack_reads_and_declares_what_it_expects(name: str) -> None:
    loaded = dv.load_pack(name=f"demo-{name}")
    assert loaded.sources
    assert set(loaded.hunts) == set(loaded.hunt_limits)
    ceiling = 6 * (publish_compose.DEMO_MAX_DETECTIONS + 1)
    assert all(
        spec.get("max_alerts") == ceiling for spec in loaded.hunt_limits.values()
    )
    assert set(loaded.parsed) <= set(loaded.sources)
    for source in [*loaded.replayed, *loaded.polled]:
        assert loaded.parsed.get(source, {}).get("fields"), (
            f"{source} names no expected field"
        )
    for spec in loaded.parsed.values():
        for field in spec["fields"]:
            assert re.fullmatch(dv.FIELD_PATH.pattern, field)


def test_the_base_demo_reads_and_declares_what_it_expects() -> None:
    loaded = dv.load_pack(name="demo")
    assert loaded.intake == "demo-syslog"
    assert set(loaded.hunts) == set(loaded.hunt_limits)
    for source in loaded.replayed:
        assert loaded.parsed.get(source, {}).get("fields")


def test_a_pack_with_no_verify_file_is_refused(tmp_path) -> None:
    (tmp_path / "demo.json").write_text(
        '{"sources": [], "console": {"dashboards": [], "searches": []}}'
    )
    with pytest.raises(dv.VerifyError, match="verify.json"):
        dv.load_pack(name="demo-x", root=tmp_path)


def test_a_wait_scale_of_zero_tries_each_check_once() -> None:
    sleeps: list[float] = []
    result = dv.verify(
        only=["landed"],
        pack=pack(),
        stack=Answers(landing({"okta": 0})),
        sleep=sleeps.append,
        wait_scale=0,
    )[0]

    assert result.failures
    assert sleeps == []


def test_search_runs_a_search_that_may_return_nothing_and_asks_only_that_it_ran() -> (
    None
):
    answer = console(dashboards=[], searches=[SEARCH, {**SEARCH, "name": "demo-t-b"}])
    stack = Answers(lambda request: answer(request, lambda sql: rows(3)))
    result = only(
        "search",
        pack=pack(searches=["demo-t-live", "demo-t-b"], zero_ok={"demo-t-live"}),
        stack=stack,
    )

    assert not result.failures
    assert result.detail.startswith("1 saved searches returned rows")
    assert "1 that may return none ran without an error" in result.detail


def test_search_accepts_no_rows_from_a_search_that_may_return_none_but_not_an_error() -> (
    None
):
    answer = console(dashboards=[], searches=[SEARCH])
    empty = Answers(lambda request: answer(request, lambda sql: rows(0)))
    broken = Answers(lambda request: answer(request, lambda sql: REFUSED))
    waiting = pack(zero_ok={"demo-t-live"})

    assert not only("search", pack=waiting, stack=empty).failures
    failed = only("search", pack=waiting, stack=broken)
    assert [failure.detail.split(" ")[0] for failure in failed.failures] == [
        "demo-t-live"
    ]


@pytest.mark.parametrize(
    ("expression", "wanted"),
    [
        ("_json.a.b", "toString(_json.a.b)"),
        ("toString(_json.a.b)", "toString(_json.a.b)"),
        (
            "arrayStringConcat(dynamicElement(_json.attrs.path, 'Array(Int64)'), ' ')",
            "arrayStringConcat(dynamicElement(toString(_json.attrs.path), 'Array(Int64)'), ' ')",
        ),
        ("_json_other.a", "_json_other.a"),
        ("_timestamp", "_timestamp"),
    ],
)
def test_a_search_is_written_as_the_console_sends_it(
    expression: str, wanted: str
) -> None:
    assert dv.console_form(expression=expression) == wanted


def test_a_search_that_reads_a_json_path_as_a_dynamic_fails_as_it_does_in_the_console() -> (
    None
):
    typed = {**SEARCH, "select": "dynamicElement(_json.attrs.path, 'Array(Int64)')"}
    answer = console(dashboards=[], searches=[typed])

    def run(sql: str) -> dv.Reply:
        return REFUSED if "dynamicElement(toString(" in sql else rows(3)

    stack = Answers(lambda request: answer(request, run))
    result = only("search", pack=pack(zero_ok={"demo-t-live"}), stack=stack)

    assert "dynamicElement(toString(_json.attrs.path)" in result.failures[0].detail


def test_a_waiting_source_is_named_by_fetchers_and_only_its_rows_leave_the_default_table_count() -> (
    None
):
    waiting = pack(
        fetchers=["wiki"],
        waiting_sources=["wiki"],
        waiting_reason="a loader fix",
    )

    def answer(request: dv.Request) -> dv.Reply:
        query = request.payload["query"]
        if "dfe.`main`" in query:
            return rows(
                0 if "NOT startsWith(toString(_source), 'wiki')" in query else 5
            )
        return rows(0)

    stack = Answers(answer)

    fetchers = only("fetchers", pack=waiting, stack=stack)
    unclaimed = only("unclaimed", pack=waiting, stack=stack)

    assert fetchers.skipped
    assert "skipped wiki: waiting on a loader fix" in fetchers.detail
    assert not unclaimed.failures
    assert "wiki rows left out of main" in unclaimed.detail


def test_another_source_s_row_in_the_default_table_still_fails_while_one_waits() -> (
    None
):
    waiting = pack(waiting_sources=["wiki"], waiting_reason="a loader fix")
    stack = Answers(
        lambda request: rows(2) if "dfe.`main`" in request.payload["query"] else rows(0)
    )

    unclaimed = only("unclaimed", pack=waiting, stack=stack)

    assert "dfe.`main` holds 2 rows no source claimed" in unclaimed.failures[0].detail


def test_a_waiting_entry_fails_once_the_source_holds_rows_of_its_own() -> None:
    waiting = pack(waiting_sources=["wiki"], waiting_reason="a loader fix")
    stack = Answers(
        lambda request: rows(3) if "dfe.`wiki`" in request.payload["query"] else rows(0)
    )

    unclaimed = only("unclaimed", pack=waiting, stack=stack)

    assert "no longer waits on a loader fix" in unclaimed.failures[0].detail
    assert "Remove it from verify.json waiting" in unclaimed.failures[0].detail


def test_search_names_why_the_console_login_could_not_be_read() -> None:
    stack = Answers(lambda request: rows(3))
    stack.login_fault = "secrets-init login exited 1 and printed no line"

    result = only("search", pack=pack(), stack=stack)

    assert (
        result.failures[0].detail == "secrets-init login exited 1 and printed no line"
    )


def test_search_runs_every_saved_search_and_names_the_one_that_returns_nothing() -> (
    None
):
    second = {**SEARCH, "name": "demo-t-b", "where": "empty = 1"}
    answer = console(dashboards=[], searches=[SEARCH, second])
    stack = Answers(
        lambda request: answer(
            request, lambda sql: rows(0) if "empty = 1" in sql else rows(3)
        )
    )

    result = only(
        "search", pack=pack(searches=["demo-t-live", "demo-t-b"]), stack=stack
    )

    assert [failure.detail.split(" ")[0] for failure in result.failures] == ["demo-t-b"]
