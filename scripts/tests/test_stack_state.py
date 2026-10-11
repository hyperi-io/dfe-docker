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


def test_the_restore_names_the_project_directory_and_files_it_came_from() -> None:
    [restore] = _stack_state.restores(
        _stack_state.containers([_document("dfe-engine")])
    )

    assert restore.command() == [
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
        "*",
        "up",
        "-d",
        "dfe-engine",
    ]


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
