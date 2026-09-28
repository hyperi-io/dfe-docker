#  Project:      dfe-docker
#  File:         tests/test_detection.py
#  Purpose:      Assert how the rules-and-hunts e2e test builds and judges its hunt
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The judging half of the rules-and-hunts e2e test, which needs no stack to check.

The readers are fed the tab-separated rows ClickHouse returns for the landing and
detection tables, and the verdict is driven through every way a hunt can be wrong.
"""

import json

import pytest

import _detection
from _common import REPO_ROOT

_E2E_TESTS = REPO_ROOT / "tests" / "e2e" / "e2e-tests.yaml"
_RULE = "run-1-rules-and-hunts-rule"
_LANDED = {
    "matching-000": ["u-m0"],
    "matching-001": ["u-m1"],
    "other-000": ["u-o0"],
}


def _hit(uuid: str, rule: str = _RULE, severity: str = "high") -> _detection.Detection:
    return _detection.Detection(matched_uuid=uuid, rule_id=rule, severity=severity)


def _verdict(found: list[_detection.Detection], landed=None) -> list[str]:
    return _detection.verdict(
        landed=_LANDED if landed is None else landed,
        found=found,
        matching=["matching-000", "matching-001"],
        other=["other-000"],
        rule=_RULE,
    )


def test_exactly_the_matching_rows_is_a_clean_verdict() -> None:
    assert _verdict([_hit("u-m0"), _hit("u-m1")]) == []


def test_a_matching_row_with_no_detection_is_named() -> None:
    problems = _verdict([_hit("u-m0")])

    assert problems == ["matching events with no detection: matching-001"]


def test_a_detected_near_miss_is_named() -> None:
    problems = _verdict([_hit("u-m0"), _hit("u-m1"), _hit("u-o0")])

    assert problems == ["events the rule must not match were detected: other-000"]


def test_a_detection_of_a_row_this_run_never_sent_is_named() -> None:
    problems = _verdict([_hit("u-m0"), _hit("u-m1"), _hit("u-elsewhere")])

    assert problems == ["detections of rows this run did not send: u-elsewhere"]


def test_a_row_detected_twice_is_named() -> None:
    problems = _verdict([_hit("u-m0"), _hit("u-m0"), _hit("u-m1")])

    assert problems == ["rows detected more than once: matching-000"]


def test_a_duplicated_landing_needs_every_copy_detected() -> None:
    landed = {**_LANDED, "matching-001": ["u-m1", "u-m1-copy"]}

    assert _verdict([_hit("u-m0"), _hit("u-m1")], landed) == [
        "matching events with no detection: matching-001"
    ]
    assert _verdict([_hit("u-m0"), _hit("u-m1"), _hit("u-m1-copy")], landed) == []


def test_an_event_that_never_landed_cannot_be_judged() -> None:
    landed = {"matching-000": ["u-m0"], "other-000": ["u-o0"]}

    problems = _verdict([_hit("u-m0")], landed)

    assert problems == ["never landed, so could not be judged: matching-001"]


def test_no_detections_at_all_names_every_matching_event() -> None:
    assert _verdict([]) == [
        "matching events with no detection: matching-000, matching-001"
    ]


def test_another_rule_or_severity_is_named() -> None:
    problems = _verdict([_hit("u-m0", rule="other-rule"), _hit("u-m1", severity="low")])

    assert problems == [
        "detections under another rule id: other-rule",
        "detections at another severity than high: low",
    ]


def test_landed_rows_are_read_by_label() -> None:
    tsv = "u-1\tmatching-000\nu-2\tother-000\nu-3\tmatching-000\n\n"

    assert _detection.landed_labels(tsv) == {
        "matching-000": ["u-1", "u-3"],
        "other-000": ["u-2"],
    }


@pytest.mark.parametrize("line", ["", "u-1", "u-1\t", "\tmatching-000"])
def test_a_row_without_both_a_uuid_and_a_label_is_not_a_landing(line: str) -> None:
    assert _detection.landed_labels(line) == {}


def test_detections_are_read_from_three_columns() -> None:
    tsv = f"u-1\t{_RULE}\thigh\nshort\trow\n"

    assert _detection.detections(tsv) == [_hit("u-1")]


def test_every_event_carries_the_run_marker_and_its_own_label() -> None:
    bodies = _detection.labelled_events(
        marker="run-1",
        test_name="rules-and-hunts",
        source="main",
        events={
            _detection.OTHER: [{"user_name": "alice"}],
            _detection.MATCHING: [{"user_name": "root"}, {"user_name": "root"}],
        },
    )

    labels = [label for label, _ in bodies]
    decoded = [json.loads(body) for _, body in bodies]
    assert labels == ["matching-000", "matching-001", "other-000"]
    assert [event["_tags"]["event"] for event in decoded] == labels
    assert {event["_tags"]["marker"] for event in decoded} == {"run-1"}
    assert {event["_source"] for event in decoded} == {"main"}
    assert [event["user_name"] for event in decoded] == ["root", "root", "alice"]


def test_the_rule_is_held_to_the_run_by_its_marker() -> None:
    sql = _detection.rule_sql(
        database="dfe", table="main", marker="run-'1", where="a = 1 OR b = 2"
    )

    assert sql == (
        "SELECT * FROM dfe.main WHERE toString(`_tags`.marker) = 'run-\\'1' "
        "AND (a = 1 OR b = 2)"
    )


def test_the_rule_request_carries_the_held_sql_at_the_verdicts_severity() -> None:
    request = _detection.rule_request(
        name="r", database="dfe", table="main", marker="run-1", where="a = 1"
    )

    assert request["name"] == "r"
    assert request["severity"] == _detection.SEVERITY
    assert request["source_type"] == "raw"
    assert request["user_sql"] == _detection.rule_sql(
        database="dfe", table="main", marker="run-1", where="a = 1"
    )


def test_a_rule_stored_over_the_source_without_errors_is_no_fault() -> None:
    body = {"rule": {"source_db": "dfe", "source_table": "main"}, "sql_errors": []}

    assert (
        _detection.rule_fault(name="r", status=201, body=body, source="dfe.main") == ""
    )


@pytest.mark.parametrize(
    ("status", "body", "fault"),
    [
        (422, {"detail": "bad"}, "POST /rules returned HTTP 422"),
        (201, "not json", "POST /rules returned HTTP 201: not json"),
        (
            201,
            {"rule": {"source_db": "dfe", "source_table": "other"}, "sql_errors": []},
            "the engine stored rule 'r' over 'dfe.other'",
        ),
        (
            201,
            {"rule": {"source_db": "dfe", "source_table": "main"}, "sql_errors": ["x"]},
            "with SQL errors ['x']",
        ),
    ],
)
def test_a_rule_that_will_not_run_over_the_source_is_a_fault(
    status: int, body: object, fault: str
) -> None:
    assert fault in _detection.rule_fault(
        name="r", status=status, body=body, source="dfe.main"
    )


@pytest.mark.parametrize(
    ("status", "body", "alive"),
    [
        (200, {"running": True}, True),
        (200, {"running": False}, False),
        (200, {"running": "true"}, False),
        (503, {"running": True}, False),
        (200, "not json", False),
    ],
)
def test_only_a_200_saying_running_true_is_a_live_runner(
    status: int, body: object, alive: bool
) -> None:
    assert _detection.runner_alive(status=status, body=body) is alive


def test_the_hunt_row_is_found_by_exact_name() -> None:
    body = {"items": [{"name": "h-2", "last_run": 1}, {"name": "h", "last_run": 2}]}

    assert _detection.hunt_row(status=200, body=body, name="h") == {
        "name": "h",
        "last_run": 2,
    }


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, {"items": [{"name": "h-2"}]}), (200, {"items": None}), (401, {}), (0, "")],
)
def test_no_matching_row_or_no_answer_is_no_hunt_row(status: int, body: object) -> None:
    assert _detection.hunt_row(status=status, body=body, name="h") is None


@pytest.mark.parametrize(
    ("now", "wait"),
    [
        (1000.0, 1.5),
        (1001.5, 0.0),
        (1005.0, 0.0),
        (990.0, _detection.QUEUE_DELAY_CAP_SECONDS),
    ],
)
def test_the_queue_waits_until_a_whole_second_past_the_last_row(
    now: float, wait: float
) -> None:
    assert _detection.queue_delay(last_load_ms=1_000_500, now=now) == wait


def test_a_202_queued_run_names_its_fire() -> None:
    body = {"queued": True, "requested_fire": 1790000001, "poll_seconds": 15.0}

    assert _detection.queued_fire(status=202, body=body) == 1790000001


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (501, {"detail": "not implemented"}),
        (202, {"queued": False, "requested_fire": 1}),
        (202, {"requested_fire": 1}),
        (202, {"queued": True, "requested_fire": None}),
        (202, "queued"),
    ],
)
def test_anything_but_a_queued_202_is_no_fire(status: int, body: object) -> None:
    assert _detection.queued_fire(status=status, body=body) is None


@pytest.mark.parametrize(
    ("row", "covered"),
    [
        ({"last_run": 1001}, True),
        # The window ends before its fire, so a row loaded on the second is outside it.
        ({"last_run": 1000}, False),
        ({"last_run": 999}, False),
        ({"last_run": None}, False),
        (None, False),
    ],
)
def test_coverage_needs_a_window_ending_after_the_last_row(
    row: dict | None, covered: bool
) -> None:
    assert _detection.covers(row, 1_000_000) is covered


def test_a_window_ending_on_the_queued_fire_is_the_queued_run() -> None:
    assert _detection.set_by({"last_run": 7}, 7) == "the queued run"
    assert _detection.set_by({"last_run": 8}, 7) == "a scheduled fire"
    assert _detection.set_by({"last_run": 8}, None) == "a scheduled fire"


def test_the_run_state_says_when_the_list_did_not_answer() -> None:
    assert _detection.run_state(None) == "hunts list did not answer"
    assert (
        _detection.run_state({"last_run": 5, "run_requested": True})
        == "last_run=5 run_requested=True"
    )


def test_the_landed_and_last_load_reads_are_held_to_the_run() -> None:
    held = "toString(`_tags`.marker) = 'run-\\'1'"

    landed = _detection.landed_sql(database="dfe", table="main", marker="run-'1")
    last = _detection.last_load_sql(database="dfe", table="main", marker="run-'1")

    assert landed.startswith(
        f"SELECT toString(_uuid), {_detection.LABEL_EXPRESSION} FROM dfe.main"
    )
    assert held in landed
    assert landed.endswith("FORMAT TabSeparated")
    assert held in last
    assert "max(_timestamp_load)" in last


@pytest.mark.parametrize(
    ("raw", "millis"), [("1790000000500", 1790000000500), ("", None), ("x", None)]
)
def test_the_last_load_is_read_or_reported_unreadable(
    raw: str, millis: int | None
) -> None:
    assert _detection.last_load_ms(raw) == millis


def test_the_detection_reads_are_held_to_the_hunt() -> None:
    count = _detection.detection_count_sql(target="dfe.detection", hunt="h'1")
    rows = _detection.detections_sql(target="dfe.detection", hunt="h'1")

    assert count == "SELECT count() FROM dfe.detection WHERE hunt_name = 'h\\'1'"
    assert rows == (
        "SELECT toString(matched_uuid), rule_id, severity FROM dfe.detection "
        "WHERE hunt_name = 'h\\'1' FORMAT TabSeparated"
    )


def test_each_sets_labels_are_the_ones_labelled_events_gives() -> None:
    events = {_detection.MATCHING: [{}, {}], _detection.OTHER: [{}]}

    labels = [
        label
        for label, _ in _detection.labelled_events(
            marker="m", test_name="t", source="main", events=events
        )
    ]

    assert _detection.set_labels(events, _detection.MATCHING) == labels[:2]
    assert _detection.set_labels(events, _detection.OTHER) == labels[2:]


def test_the_hunt_names_one_rule_over_the_source_into_the_target() -> None:
    request = _detection.hunt_request(
        name="h", rule="r", customer="e2e", source="dfe.main", target="dfe.detection"
    )

    assert request["rules"] == ["r"]
    assert request["global_source_table_name"] == "dfe.main"
    assert request["global_target_table_name"] == "dfe.detection"
    assert request["cron"] == _detection.HUNT_CRON


def test_the_suite_defines_a_detection_test_with_both_sets() -> None:
    text = _E2E_TESTS.read_text(encoding="utf-8")
    block = text.split("- name: rules-and-hunts", 1)[1].split("- name:", 1)[0]

    for kind in (_detection.MATCHING, _detection.OTHER):
        path = next(
            line.split(": ", 1)[1].strip()
            for line in block.splitlines()
            if line.strip().startswith(f"{kind}: ")
        )
        events = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
        assert events, f"{path} carries no events"
        assert all(json.loads(line) for line in events)
