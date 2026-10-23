#  Project:      dfe-docker
#  File:         tests/test_stack_state.py
#  Purpose:      Assert how the e2e suite reads a stack and plans to start it again
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The e2e suite stops every service, so it must put back exactly what it found.

Driven from `docker inspect` documents shaped as docker writes them: which
containers come back, from which compose files, and when the project counts as
different from how it was found.
"""

import pytest

import _stack_state

_FILES = "/opt/dfe/docker-compose.yml,/opt/dfe/docker-compose.tls.yml"


def _document(
    service: str,
    *,
    running: bool = True,
    exit_code: int = 0,
    restart: str = "unless-stopped",
    config_hash: str = "aaaa",
    files: str = _FILES,
    oneoff: str = "False",
) -> dict:
    return {
        "Config": {
            "Labels": {
                _stack_state.SERVICE_LABEL: service,
                _stack_state.PROJECT_LABEL: "dfe-docker",
                _stack_state.WORKING_DIR_LABEL: "/opt/dfe",
                _stack_state.CONFIG_FILES_LABEL: files,
                _stack_state.CONFIG_HASH_LABEL: config_hash,
                _stack_state.ONEOFF_LABEL: oneoff,
            }
        },
        "State": {"Running": running, "ExitCode": exit_code},
        "HostConfig": {"RestartPolicy": {"Name": restart}},
    }


def test_a_running_service_and_a_finished_one_shot_both_come_back() -> None:
    found = _stack_state.containers(
        [
            _document("dfe-engine"),
            _document("dlq-init", running=False, restart="no"),
        ]
    )

    [restore] = _stack_state.restores(found)

    assert restore.services == ("dfe-engine", "dlq-init")


@pytest.mark.parametrize(
    "document",
    [
        _document("dfe-loader", running=False),
        _document("dlq-init", running=False, exit_code=1, restart="no"),
        _document("dfe-engine", oneoff="True"),
    ],
    ids=["stopped-service", "failed-one-shot", "compose-run"],
)
def test_what_was_not_running_or_finished_cleanly_stays_down(document: dict) -> None:
    assert _stack_state.restores(_stack_state.containers([document])) == []


def test_the_restore_names_the_project_directory_files_and_profiles() -> None:
    [restore] = _stack_state.restores(
        _stack_state.containers([_document("dfe-engine")])
    )

    assert restore.command(["core", "clickhouse", "core"]) == [
        "docker",
        "compose",
        "--project-name",
        "dfe-docker",
        "--project-directory",
        "/opt/dfe",
        "-f",
        "/opt/dfe/docker-compose.yml",
        "-f",
        "/opt/dfe/docker-compose.tls.yml",
        "--profile",
        "clickhouse",
        "--profile",
        "core",
        "up",
        "-d",
        "dfe-engine",
    ]


# The shape that matters: dfe-engine depends on both brokers, each optional and
# gated by its own profile, so enabling every profile starts both.
_MODEL = {
    "services": {
        "clickhouse": {"profiles": ["clickhouse"]},
        "dfe-engine": {"profiles": ["core"]},
        "kafka-apache": {"profiles": ["kafka-apache"]},
        "kafka-redpanda": {"profiles": ["kafka-redpanda"]},
        "dfe-instance": {},
    }
}


def test_only_the_restored_services_profiles_are_enabled() -> None:
    profiles = _stack_state.profiles_of(_MODEL, ["dfe-engine", "clickhouse"])

    assert profiles == {"core", "clickhouse"}


def test_a_service_with_no_profile_or_unknown_to_the_model_adds_none() -> None:
    assert _stack_state.profiles_of(_MODEL, ["dfe-instance", "gone"]) == set()


def test_the_config_command_reads_every_service_the_files_define() -> None:
    [restore] = _stack_state.restores(
        _stack_state.containers([_document("dfe-engine")])
    )

    assert restore.config()[-5:] == ["--profile", "*", "config", "--format", "json"]
    assert "*" not in restore.command(["core"])


def test_a_compose_file_that_is_gone_is_left_out_and_named() -> None:
    found = _stack_state.containers([_document("clickhouse")])

    [restore] = _stack_state.restores(
        found, exists=lambda path: not (path.endswith("tls.yml"))
    )

    assert restore.config_files == ("/opt/dfe/docker-compose.yml",)
    assert restore.missing == ("/opt/dfe/docker-compose.tls.yml",)
    assert "/opt/dfe/docker-compose.tls.yml" not in restore.command()


# A deleted source's transform and the receiver, both created while the
# instances fragment declared that transform. Deleting the source empties the
# engine's index, and the next profile resolution removes the fragment.
_INSTANCES = "/opt/dfe/.tmp/docker-compose.instances.yml"
_WITH_INSTANCES = f"/opt/dfe/docker-compose.yml,{_INSTANCES}"
_ORPHAN = "dfe-transform-vrl-fb0c4e9a17"
_WITHOUT_INSTANCES = {"services": {"dfe-receiver": {"profiles": ["dfe"]}}}


def _orphaned_restore() -> tuple[list[_stack_state.Container], _stack_state.Restore]:
    found = _stack_state.containers(
        [
            _document("dfe-receiver", files=_WITH_INSTANCES),
            _document(_ORPHAN, files=_WITH_INSTANCES),
        ]
    )
    [restore] = _stack_state.restores(found, exists=lambda path: path != _INSTANCES)
    return found, _stack_state.still_defined(restore, _WITHOUT_INSTANCES)


def test_a_service_its_files_no_longer_define_is_left_out_of_the_up() -> None:
    # Compose refuses a whole `up` that names one, which kept the receiver down.
    _, restore = _orphaned_restore()

    profiles = _stack_state.profiles_of(_WITHOUT_INSTANCES, restore.services)

    assert restore.command(profiles)[-3:] == ["up", "-d", "dfe-receiver"]
    assert restore.undefined == (_ORPHAN,)


def test_a_service_left_out_of_the_up_is_not_reported_missing() -> None:
    found, restore = _orphaned_restore()
    after = _stack_state.containers(
        [_document("dfe-receiver", files="/opt/dfe/docker-compose.yml")]
    )

    assert _stack_state.differences(found, after, {}, restore.undefined) == []


def test_a_restore_whose_files_define_none_of_its_services_names_none() -> None:
    [restore] = _stack_state.restores(
        _stack_state.containers([_document(_ORPHAN, files=_WITH_INSTANCES)]),
        exists=lambda path: path != _INSTANCES,
    )

    narrowed = _stack_state.still_defined(restore, _WITHOUT_INSTANCES)

    assert narrowed.services == ()
    assert narrowed.undefined == (_ORPHAN,)


def test_a_service_the_files_still_define_is_held_to_the_restore() -> None:
    found = _stack_state.containers([_document("dfe-receiver"), _document(_ORPHAN)])

    problems = _stack_state.differences(found, [], {}, (_ORPHAN,))

    assert problems == ["'dfe-receiver' has no container"]


def test_services_started_from_different_files_get_one_up_each() -> None:
    found = _stack_state.containers(
        [
            _document("dfe-engine"),
            _document("dfe-ui", files="/opt/dfe/docker-compose.yml"),
        ]
    )

    assert [restore.services for restore in _stack_state.restores(found)] == [
        ("dfe-ui",),
        ("dfe-engine",),
    ]


def test_a_project_put_back_as_found_shows_no_difference() -> None:
    found = _stack_state.containers(
        [_document("dfe-engine"), _document("dlq-init", running=False, restart="no")]
    )

    assert _stack_state.differences(found, found) == []


def test_found_nothing_and_left_nothing_shows_no_difference() -> None:
    assert _stack_state.differences([], []) == []


@pytest.mark.parametrize(
    ("after", "problem"),
    [
        ([], "'dfe-engine' has no container"),
        ([_document("dfe-engine", running=False)], "'dfe-engine' is not running"),
        (
            [_document("dfe-engine", config_hash="bbbb")],
            "'dfe-engine' is configured differently",
        ),
        (
            [_document("dfe-engine"), _document("clickhouse")],
            "'clickhouse' is running and was not",
        ),
    ],
    ids=["gone", "stopped", "reconfigured", "extra"],
)
def test_each_way_a_project_can_differ_is_named(after: list, problem: str) -> None:
    found = _stack_state.containers([_document("dfe-engine")])

    problems = _stack_state.differences(found, _stack_state.containers(after))

    assert len(problems) == 1
    assert problems[0].startswith(problem)


def test_a_stack_left_up_after_finding_none_is_a_difference() -> None:
    problems = _stack_state.differences(
        [], _stack_state.containers([_document("clickhouse")])
    )

    assert problems == ["'clickhouse' is running and was not"]


def test_the_hash_command_takes_the_up_profiles_and_prints_every_service() -> None:
    [restore] = _stack_state.restores(
        _stack_state.containers([_document("dfe-engine")])
    )

    command = restore.hashes(["core", "clickhouse", "core"])

    assert command[: len(restore.compose())] == restore.compose()
    assert command[len(restore.compose()) :] == [
        "--profile",
        "clickhouse",
        "--profile",
        "core",
        "config",
        "--hash",
        "*",
    ]


def test_config_hashes_keeps_only_the_named_services_compose_printed() -> None:
    output = "clickhouse 1111\ndfe-engine 2222\ndfe-ui 3333\n\nWARN not a hash line\n"

    hashes = _stack_state.config_hashes(output, ["dfe-engine", "clickhouse", "gone"])

    assert hashes == {"clickhouse": "1111", "dfe-engine": "2222"}


def test_a_service_found_on_an_old_config_comes_back_on_its_files_cleanly() -> None:
    found = _stack_state.containers([_document("dfe-engine", config_hash="aaaa")])
    after = _stack_state.containers([_document("dfe-engine", config_hash="bbbb")])

    assert _stack_state.differences(found, after, {"dfe-engine": "bbbb"}) == []


def test_a_config_change_made_during_the_run_is_still_a_difference() -> None:
    found = _stack_state.containers([_document("dfe-engine", config_hash="aaaa")])
    after = _stack_state.containers([_document("dfe-engine", config_hash="cccc")])

    problems = _stack_state.differences(found, after, {"dfe-engine": "bbbb"})

    assert problems == [
        "'dfe-engine' is configured differently (config hash bbbb became cccc)"
    ]


def test_a_service_with_no_expected_hash_is_held_to_its_container() -> None:
    found = _stack_state.containers([_document("dfe-engine", config_hash="aaaa")])
    after = _stack_state.containers([_document("dfe-engine", config_hash="bbbb")])

    problems = _stack_state.differences(found, after, {"clickhouse": "bbbb"})

    assert problems == [
        "'dfe-engine' is configured differently (config hash aaaa became bbbb)"
    ]


def test_stale_names_only_restored_services_their_files_have_moved_past() -> None:
    found = _stack_state.containers(
        [
            _document("dfe-engine", config_hash="aaaa"),
            _document("clickhouse", config_hash="cccc"),
            _document("dfe-loader", running=False, config_hash="dddd"),
            _document("dfe-ui", config_hash="eeee"),
        ]
    )
    expected = {"dfe-engine": "bbbb", "clickhouse": "cccc", "dfe-loader": "ffff"}

    assert _stack_state.stale(found, expected) == [
        "'dfe-engine' runs config hash aaaa, its files give bbbb"
    ]


_SINGLE_GRPC_ENGINE = {
    "DFE_PROFILE": "docker-single",
    "DFE_TRANSPORT_DEFAULT": "direct",
}


@pytest.mark.parametrize(
    ("engine_environment", "environ"),
    [
        (
            _SINGLE_GRPC_ENGINE,
            {"DFE_STACK_PROFILE": "docker-single", "DFE_TRANSPORT_DEFAULT": "direct"},
        ),
        ({"DFE_PROFILE": "docker-slim", "DFE_TRANSPORT_DEFAULT": "direct"}, {}),
        ({}, {"DFE_STACK_PROFILE": "docker-single", "DFE_TRANSPORT_DEFAULT": "bus"}),
    ],
    ids=["same-profile", "compose-defaults", "no-engine"],
)
def test_a_run_that_keeps_the_stack_on_its_profile_is_no_change(
    engine_environment: dict, environ: dict
) -> None:
    assert (
        _stack_state.profile_change(
            engine_environment=engine_environment, environ=environ
        )
        == ""
    )


@pytest.mark.parametrize(
    ("environ", "given"),
    [
        (
            {"DFE_STACK_PROFILE": "docker-single", "DFE_TRANSPORT_DEFAULT": "bus"},
            "single-kafka",
        ),
        (
            {"DFE_STACK_PROFILE": "docker-slim", "DFE_TRANSPORT_DEFAULT": "direct"},
            "slim-grpc",
        ),
    ],
    ids=["other-transport", "other-tier"],
)
def test_a_run_that_would_move_the_stack_names_the_profile_to_rerun_with(
    environ: dict, given: str
) -> None:
    assert _stack_state.profile_change(
        engine_environment=_SINGLE_GRPC_ENGINE, environ=environ
    ) == (
        f"the stack runs profile 'single-grpc' but these make flags give {given!r}. "
        "Rerun with DFE_PROFILE=single-grpc"
    )
