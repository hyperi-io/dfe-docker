#  Project:      dfe-docker
#  File:         tests/test_outage.py
#  Purpose:      Assert how the outage tests judge answers, landings, containers and lag
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The judging half of the outage e2e tests, which needs no stack to check.

The load is driven against a real HTTP server on loopback, and the readers are
fed the documents docker and rpk actually print.
"""

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import _outage
import resolve_profile
from _common import CONFIG_DIR, SERVICE_PROFILES_FILE


def test_a_record_marker_round_trips_to_its_sequence_number() -> None:
    marker = _outage.record_marker("e2e-1-kafka-outage", 42)

    assert _outage.seq_of(marker, "e2e-1-kafka-outage") == 42


@pytest.mark.parametrize(
    "marker",
    [
        "e2e-1-kafka-outage-longer-000001",
        "e2e-2-kafka-outage-000001",
        "e2e-1-kafka-outage-",
        "e2e-1-kafka-outage-12a",
        "",
    ],
)
def test_a_marker_from_another_run_or_test_is_not_this_loads(marker: str) -> None:
    assert _outage.seq_of(marker, "e2e-1-kafka-outage") is None


def _sent(
    seq: int,
    status: int | None,
    *,
    phase: str = "during",
    source: str = "main",
    started: float = 0.0,
    seconds: float = 0.1,
):
    return _outage.Sent(
        seq=seq,
        source=source,
        phase=phase,
        status=status,
        error="" if status is not None else "ConnectionRefusedError",
        seconds=seconds,
        started=started,
    )


def test_a_plan_with_only_a_service_is_a_graceful_stop() -> None:
    plan = _outage.outage_plan({"service": "dfe-loader"}, 60)

    assert plan.service == "dfe-loader"
    assert plan.seconds == 60
    assert plan.signal == "TERM"
    assert not (plan.pause_service or plan.expect_loss or plan.spool_service)


def test_a_kill_under_a_pause_is_read_whole() -> None:
    plan = _outage.outage_plan(
        {
            "service": "dfe-receiver",
            "signal": "kill",
            "seconds": "20",
            "pause": {"service": "kafka", "seconds": 10},
            "expect_loss": True,
            "spool": {"service": "dfe-receiver", "path": "/tmp/spool"},
            "sources": {"main": "tests/e2e/data/events.jsonl"},
        },
        60,
    )

    assert plan.signal == "KILL"
    assert plan.seconds == 20
    assert (plan.pause_service, plan.pause_seconds) == ("kafka", 10)
    assert plan.expect_loss
    assert (plan.spool_service, plan.spool_path) == ("dfe-receiver", "/tmp/spool")
    assert not plan.spool_must_stay_empty
    assert plan.sources == {"main": "tests/e2e/data/events.jsonl"}


def test_a_pause_alone_is_an_outage_that_takes_nothing_down() -> None:
    plan = _outage.outage_plan(
        {"pause": {"service": "dfe-loader", "seconds": 40}, "expect_refusals": True},
        60,
    )

    assert plan.service == ""
    assert plan.seconds == 0
    assert plan.pause_seconds == 40
    assert plan.expect_refusals


@pytest.mark.parametrize(
    "outage,complaint",
    [
        ({}, "no 'service'"),
        ({"service": "dfe-loader", "expect_los": True}, "unknown key(s): expect_los"),
        ({"service": "dfe-loader", "signal": "HUP"}, "'signal' must be one of"),
        ({"service": "dfe-loader", "seconds": 0}, "whole number above 0"),
        ({"service": "dfe-loader", "seconds": True}, "whole number above 0"),
        ({"service": "dfe-loader", "seconds": "soon"}, "whole number above 0"),
        ({"pause": {"seconds": 10}}, "'pause' names no 'service'"),
        ({"pause": {"service": "dfe-loader"}}, "'pause' 'seconds'"),
        (
            {"service": "dfe-loader", "pause": {"service": "dfe-loader", "seconds": 5}},
            "cannot freeze the service it takes down",
        ),
        (
            {"pause": {"service": "dfe-loader", "seconds": 5}, "seconds": 20},
            "need a 'service'",
        ),
        (
            {"service": "dfe-loader", "pause": {"service": "kafka", "secs": 5}},
            "unknown key(s): secs",
        ),
        (
            {"service": "dfe-loader", "spool": {"service": "dfe-receiver"}},
            "both 'service' and 'path'",
        ),
        ({"service": "dfe-loader", "expect_loss": "yes"}, "must be true or false"),
        ({"service": "dfe-loader", "sources": ["main"]}, "'sources' must map"),
    ],
)
def test_a_plan_refuses_what_it_cannot_run(outage: dict, complaint: str) -> None:
    with pytest.raises(ValueError, match=re.escape(complaint)):
        _outage.outage_plan(outage, 60)


def test_silence_is_excused_only_while_the_ingress_was_down() -> None:
    sent = [
        _sent(0, 202, started=100.0),
        _sent(1, None, started=99.0, seconds=2.0),
        _sent(2, None, started=110.0),
        _sent(3, None, started=125.0),
        _sent(4, None, started=90.0, seconds=1.0),
    ]

    excused, unexcused = _outage.unanswered(sent, (100.5, 120.0))

    assert [record.seq for record in excused] == [1, 2]
    assert [record.seq for record in unexcused] == [3, 4]


def test_a_graceful_stop_owes_an_answer_to_what_was_in_flight() -> None:
    sent = [
        _sent(1, None, started=99.0, seconds=2.0),
        _sent(2, None, started=110.0),
    ]

    excused, unexcused = _outage.unanswered(sent, (100.5, 120.0), in_flight=False)

    assert [record.seq for record in excused] == [2]
    assert [record.seq for record in unexcused] == [1]


def test_no_silence_is_excused_when_the_ingress_stayed_up() -> None:
    sent = [_sent(0, None, started=110.0), _sent(1, 503, started=111.0)]

    excused, unexcused = _outage.unanswered(sent, None)

    assert excused == []
    assert [record.seq for record in unexcused] == [0]


def test_row_counts_are_read_for_this_loads_markers_only() -> None:
    printed = (
        "e2e-1-loader-kill-grpc-000001\t1\n"
        "e2e-1-loader-kill-grpc-000002\t3\n"
        "e2e-2-loader-kill-grpc-000003\t1\n"
        "e2e-1-loader-kill-grpc-000004\tnot-a-count\n"
        "\n"
    )

    assert _outage.marker_counts(printed, "e2e-1-loader-kill-grpc") == {1: 1, 2: 3}


def test_the_landing_tally_counts_loss_and_duplicates_by_phase() -> None:
    accepted = {0: "before", 1: "during", 2: "during", 3: "after"}
    counts = {0: 1, 1: 2, 3: 3, 9: 2}
    phases = {**accepted, 9: "during"}

    landing = _outage.tally_landing(accepted, counts, phases)

    assert (landing.accepted, landing.landed) == (4, 3)
    assert landing.missing == [2]
    assert landing.lost_by_phase == {"during": 1}
    assert landing.duplicated_by_phase == {"during": 2, "after": 1}
    assert landing.duplicated == 3
    assert landing.extra_rows == 4


def test_nothing_accepted_and_nothing_landed_is_no_loss() -> None:
    landing = _outage.tally_landing({}, {}, {})

    assert (landing.accepted, landing.landed, landing.missing) == (0, 0, [])
    assert landing.duplicated == 0


def test_a_file_counts_as_written_when_new_or_grown() -> None:
    before = {"grpc/loader/0": 10, "grpc/loader/lock": 0}
    after = {"grpc/loader/0": 10, "grpc/loader/lock": 0, "grpc/loader/1": 0}

    assert _outage.grown(before, after) == ["grpc/loader/1"]
    assert _outage.grown(before, {**after, "grpc/loader/0": 11}) == [
        "grpc/loader/0",
        "grpc/loader/1",
    ]
    assert _outage.grown(before, before) == []
    assert _outage.grown({}, {}) == []


def test_only_a_2xx_obliges_the_record_to_land() -> None:
    assert _sent(1, 200).accepted
    assert _sent(2, 204).accepted
    assert not _sent(3, 503).accepted
    assert not _sent(4, 429).accepted
    assert not _sent(5, None).accepted


def test_a_request_nothing_answered_says_why() -> None:
    assert _sent(1, None).outcome == "none (ConnectionRefusedError)"
    assert _sent(2, 503).outcome == "503"


def test_answers_are_counted_per_phase() -> None:
    sent = [
        _sent(0, 200, phase="before"),
        _sent(1, 503, phase="during"),
        _sent(2, 503, phase="during"),
        _sent(3, None, phase="during"),
    ]

    table = _outage.outcomes_by_phase(sent)

    assert table["before"] == {"200": 1}
    assert table["during"] == {"503": 2, "none (ConnectionRefusedError)": 1}
    assert table["after"] == {}


def test_only_accepted_records_of_the_named_source_are_owed() -> None:
    sent = [
        _sent(0, 200, phase="before"),
        _sent(1, 503),
        _sent(2, 200, source="cisco-ios"),
        _sent(3, 200, phase="after"),
    ]

    assert _outage.accepted_by_seq(sent, "main") == {0: "before", 3: "after"}


def test_sources_alternate_and_a_short_one_repeats() -> None:
    records = _outage.interleave(
        {"main": [{"a": 1}, {"a": 2}, {"a": 3}], "ios": [{"b": 1}]}
    )

    assert [source for source, _ in records] == ["main", "ios"] * 3
    assert [event for source, event in records if source == "ios"] == [{"b": 1}] * 3


def test_a_load_with_nothing_to_send_is_refused() -> None:
    with pytest.raises(ValueError):
        _outage.interleave({"main": []})


_RUNNING = {
    "Id": "a" * 64,
    "RestartCount": 0,
    "State": {
        "Status": "running",
        "Pid": 4242,
        "StartedAt": "2026-09-24T01:00:00.000000000Z",
        "Health": {"Status": "healthy"},
    },
}


def test_a_container_that_ran_straight_through_has_no_changes() -> None:
    state = _outage.container_state(_RUNNING)

    assert state.health == "healthy"
    assert _outage.state_changes(state, state) == []


def test_a_restarted_container_is_caught_on_every_sign() -> None:
    restarted = json.loads(json.dumps(_RUNNING))
    restarted["RestartCount"] = 1
    restarted["State"]["Pid"] = 5151
    restarted["State"]["StartedAt"] = "2026-09-24T01:01:07.000000000Z"

    changes = _outage.state_changes(
        _outage.container_state(_RUNNING), _outage.container_state(restarted)
    )

    assert "restart count 0 -> 1" in changes
    assert "pid 4242 -> 5151" in changes
    assert any("started again" in change for change in changes)


def test_an_exited_container_is_not_running() -> None:
    exited = json.loads(json.dumps(_RUNNING))
    exited["State"].update(Status="exited", Pid=0)

    changes = _outage.state_changes(
        _outage.container_state(_RUNNING), _outage.container_state(exited)
    )

    assert "status is exited" in changes


def test_a_container_reports_how_its_last_process_exited() -> None:
    killed = json.loads(json.dumps(_RUNNING))
    killed["State"].update(Status="exited", Pid=0, ExitCode=137)

    assert _outage.container_state(killed).exit_code == 137
    assert _outage.container_state(_RUNNING).exit_code == 0


def test_die_and_oom_events_carry_their_exit_code() -> None:
    lines = "\n".join(
        [
            json.dumps(
                {
                    "Type": "container",
                    "Action": "die",
                    "Actor": {"ID": "a" * 64, "Attributes": {"exitCode": "101"}},
                    "time": 1790231400,
                }
            ),
            json.dumps(
                {
                    "Type": "container",
                    "Action": "oom",
                    "id": "b" * 64,
                    "time": 1790231399,
                }
            ),
            "not json",
        ]
    )

    assert _outage.exits(lines) == [
        ("a" * 64, 1790231400, "die with exit code 101"),
        ("b" * 64, 1790231399, "oom"),
    ]


def test_only_error_fatal_and_panic_lines_are_kept_from_a_log() -> None:
    log = "\n".join(
        [
            "INFO starting",
            "ERROR scalo::worker::engine::driver: Sink failed -- terminal",
            "INFO shutdown complete",
            "fatal: service error: pipeline failed",
            "thread 'main' panicked at src/main.rs:1",
            "INFO starting",
        ]
    )

    assert _outage.failure_lines(log, 2) == [
        "fatal: service error: pipeline failed",
        "thread 'main' panicked at src/main.rs:1",
    ]
    assert _outage.failure_lines("INFO fine\n", 5) == []


def test_expected_consumers_count_each_subscriber_of_a_topic(tmp_path) -> None:
    archiver = tmp_path / "archiver.yaml"
    archiver.write_text("kafka:\n  topics:\n    - main_land\n", encoding="utf-8")
    vrl = tmp_path / "vrl.yaml"
    vrl.write_text(
        "source:\n  topics:\n    - main_land\nsink:\n  topic: main_load\n",
        encoding="utf-8",
    )
    vector = tmp_path / "vector.yaml"
    vector.write_text("dfe_source: main\n", encoding="utf-8")
    receiver = tmp_path / "receiver.yaml"
    receiver.write_text("kafka:\n  brokers:\n    - kafka:9092\n", encoding="utf-8")

    counts = _outage.expected_consumers([archiver, vrl, vector, receiver])

    assert counts == {"main_land": 3}


def test_a_pattern_subscriber_is_owed_a_commit_on_each_topic_it_reads(
    tmp_path,
) -> None:
    loader = tmp_path / "loader.yaml"
    loader.write_text(
        "kafka:\n  group: dfe-loader\n  topic_regex: .*_(land|load)\n", encoding="utf-8"
    )
    topics = ["main_land", "main_load", "vector_land", "vector_load", "orphan_land"]

    counts = _outage.expected_consumers([loader], topics)

    assert counts == {"main_load": 1, "vector_load": 1, "orphan_land": 1}


def test_a_topic_pattern_is_searched_not_anchored() -> None:
    assert _outage.pattern_subscriptions("land", ["main_land", "main_load"]) == {
        "main_land"
    }
    assert _outage.pattern_subscriptions(
        "^strimzi\\.", ["strimzi.a", "x.strimzi.b"]
    ) == {"strimzi.a"}


def test_the_broker_outage_owes_the_loader_a_commit_on_every_load_topic() -> None:
    profiles = resolve_profile._parse_yaml(
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
    )
    services = profiles["profiles"]["kafka-resilience"]["services"]
    configs = [CONFIG_DIR / service["config_path"] for service in services.values()]
    # kafka-outage's expected_topics in tests/e2e/e2e-tests.yaml.
    topics = [
        f"{source}_{end}"
        for source in ("main", "vector", "cisco-ios")
        for end in ("land", "load")
    ]

    counts = _outage.expected_consumers(configs, topics)

    assert counts == {
        "main_land": 2,
        "vector_land": 1,
        "cisco-ios_land": 1,
        "main_load": 1,
        "vector_load": 1,
        "cisco-ios_load": 1,
    }


# What `rpk topic describe <topic> -p` printed for a three-partition topic.
_RPK_PARTITIONS = """\
PARTITION  LEADER  EPOCH  REPLICAS  LOG-START-OFFSET  HIGH-WATERMARK
0          0       3      [0]       0                 372
1          0       3      [0]       0                 279
2          0       3      [0]       0                 93
"""


def test_rpk_high_watermarks_are_summed_across_partitions() -> None:
    assert _outage.high_watermark_text(_RPK_PARTITIONS) == 744


def test_a_describe_with_no_partition_row_is_unreadable_not_empty() -> None:
    assert _outage.high_watermark_text("") is None
    assert _outage.high_watermark_text("UNKNOWN_TOPIC_OR_PARTITION") is None


def test_apache_latest_offsets_are_summed_for_the_named_topic_only() -> None:
    printed = "main_land:0:5\nmain_land:1:2\nmain_load:0:9\n"

    assert _outage.high_watermark_offsets(printed, "main_land") == 7
    assert _outage.high_watermark_offsets(printed, "vector_land") is None


def test_a_landing_topic_nothing_was_produced_to_never_reached_kafka() -> None:
    empty = (
        "PARTITION  LEADER  EPOCH  REPLICAS  LOG-START-OFFSET  HIGH-WATERMARK\n"
        "0          0       1      [0]       0                 0\n"
    )
    watermarks = {"main_land": _outage.high_watermark_text(empty)}

    problems = _outage.kafka_unreached(watermarks, {}, {})

    assert problems == ["'main_land': never reached Kafka (high watermark 0)"]


def test_an_unreadable_watermark_is_not_taken_as_a_pass() -> None:
    assert _outage.kafka_unreached({"main_land": None}, {}, {}) == [
        "'main_land': high watermark unreadable"
    ]


# What rpk printed for a run whose load bypassed Kafka: the group exists and
# never committed anything.
_NEVER_COMMITTED = (
    '[{"group_name":"dfe-transform-vrl","coordinator_partition":"__consumer_offsets/1",'
    '"state":"Empty","balancer":"","members":0,"coordinator_node":0,"total_lag":0,'
    '"partitions":[],"members_details":[]}]'
)


def test_a_group_that_never_committed_is_named_before_the_outage() -> None:
    lag = _outage.group_lag_json(_NEVER_COMMITTED)

    problems = _outage.kafka_unreached({"main_land": 12}, lag, {"main_land": 1})

    assert problems == ["'main_land': 0 of 1 consumer group(s) have committed (none)"]


def test_a_load_through_kafka_leaves_nothing_unreached() -> None:
    lag = {"dfe-transform-vrl": {"main_land": 3}, "dfe-loader": {"main_load": 0}}

    problems = _outage.kafka_unreached(
        {"main_land": 12}, lag, {"main_land": 1, "main_load": 1}
    )

    assert problems == []


# What `rpk group describe -r '.*' --format json` printed for a group one record behind.
_RPK_DESCRIBE = (
    '[{"group_name":"probe-group","coordinator_partition":"__consumer_offsets/0",'
    '"state":"Empty","balancer":"","members":0,"coordinator_node":0,"total_lag":1,'
    '"partitions":[{"partition":0,"current_offset":1,"log_start_offset":0,'
    '"log_end_offset":2,"lag":1,"topic":"probe_land","member_id":"","client_id":"",'
    '"host":""}],"members_details":[]}]'
)


def test_rpk_lag_is_read_per_group_and_topic() -> None:
    assert _outage.group_lag_json(_RPK_DESCRIBE) == {"probe-group": {"probe_land": 1}}


def test_a_partition_never_committed_on_does_not_make_a_consumer() -> None:
    uncommitted = json.loads(_RPK_DESCRIBE)
    uncommitted[0]["partitions"][0]["current_offset"] = -1

    assert _outage.group_lag_json(json.dumps(uncommitted)) == {"probe-group": {}}


# What Apache Kafka's `kafka-consumer-groups.sh --describe --all-groups` printed.
_APACHE_DESCRIBE = """Consumer group 'probe-group' has no active members.

GROUP           TOPIC           PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG             CONSUMER-ID     HOST            CLIENT-ID
probe-group     probe_land      0          2               4               2               -               -               -
probe-group     probe_land      1          0               0               0               -               -               -
"""


def test_apache_lag_is_summed_across_a_topics_partitions() -> None:
    assert _outage.group_lag_table(_APACHE_DESCRIBE) == {
        "probe-group": {"probe_land": 2}
    }


def test_an_apache_partition_with_no_commit_is_left_out() -> None:
    uncommitted = _APACHE_DESCRIBE.replace(
        "probe_land      1          0               0               0",
        "probe_land      1          -               0               -",
    )
    other = uncommitted.replace(
        "probe_land      0          2", "other_land      0          2"
    )

    assert _outage.group_lag_table(other) == {"probe-group": {"other_land": 2}}


def test_no_groups_reads_as_no_lag() -> None:
    assert _outage.group_lag_json("[]") == {}


def test_consumers_are_behind_until_each_topic_has_its_groups_at_zero() -> None:
    expected = {"main_land": 2, "main_load": 1}

    missing = _outage.consumers_behind({"loader": {"main_load": 0}}, expected)
    lagging = _outage.consumers_behind(
        {"a": {"main_land": 0}, "b": {"main_land": 7}, "loader": {"main_load": 0}},
        expected,
    )
    caught_up = _outage.consumers_behind(
        {"a": {"main_land": 0}, "b": {"main_land": 0}, "loader": {"main_load": 0}},
        expected,
    )

    assert missing == ["'main_land': 0 of 2 consumer group(s) have committed (none)"]
    assert lagging == ["'main_land': group 'b' is 7 behind"]
    assert caught_up == []


def test_a_pattern_subscriber_behind_on_a_watched_topic_is_caught() -> None:
    lag = {"a": {"main_land": 0}, "loader": {"main_load": 3}}

    assert _outage.consumers_behind(lag, {"main_land": 1}) == []
    assert _outage.consumers_behind(lag, {"main_land": 1}, ["main_load"]) == [
        "'main_load': group 'loader' is 3 behind"
    ]


class _Refusing(BaseHTTPRequestHandler):
    """Answer every odd request 503, as a receiver whose destination is gone does."""

    count = 0
    lock = threading.Lock()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with self.lock:
            type(self).count += 1
            status = 503 if self.count % 2 else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def test_the_load_records_every_answer_in_the_phase_it_was_sent() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Refusing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    load = _outage.SteadyLoad(
        url=f"http://127.0.0.1:{server.server_address[1]}/ingest",
        prefix="unit",
        records=_outage.interleave({"main": [{"message": "x"}]}),
        workers=2,
        interval=0.01,
        timeout=5,
    )
    began = time.time()
    try:
        load.start()
        time.sleep(0.2)
        load.phase = "during"
        time.sleep(0.2)
    finally:
        load.stop()
        server.shutdown()
        server.server_close()

    sent = load.sent()
    phases = {record.phase for record in sent}
    outcomes = {record.outcome for record in sent}
    assert phases == {"before", "during"}
    assert outcomes == {"200", "503"}
    assert sorted(record.seq for record in sent) == list(range(len(sent)))
    assert all(began <= record.started <= time.time() for record in sent)


def test_a_port_nothing_listens_on_is_no_answer() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    status, error = _outage.post_record(f"http://127.0.0.1:{port}/ingest", "{}", 2)

    assert status is None
    assert error == "ConnectionRefusedError"
