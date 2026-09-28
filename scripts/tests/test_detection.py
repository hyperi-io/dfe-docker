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
