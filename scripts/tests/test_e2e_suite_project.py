#  Project:      dfe-docker
#  File:         scripts/tests/test_e2e_suite_project.py
#  Purpose:      Assert the e2e suite clears a leftover suite project before it starts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A killed run can leave `<project>-e2e` and its volumes behind, so the next run removes them first.

The suite's steps around the stacks are stood in for at the suite's own function
boundaries, and every command it would run is recorded in order.
"""

import subprocess

import pytest

import test_e2e


def test_a_leftover_suite_project_is_removed_before_any_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps: list[object] = []

    def _run_cmd(args, capture=False, cwd=None):
        steps.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(test_e2e, "run_cmd", _run_cmd)
    monkeypatch.setattr(test_e2e, "project_containers", lambda: [])
    monkeypatch.setattr(test_e2e, "running_engine_environment", lambda: {})
    monkeypatch.setattr(test_e2e, "expected_hashes", lambda found: {})
    monkeypatch.setattr(test_e2e, "found_project", lambda: "dfe")
    monkeypatch.setattr(test_e2e, "stack_is_up", lambda: False)
    monkeypatch.setattr(test_e2e, "isolate_suite", lambda project: f"{project}-e2e")
    monkeypatch.setattr(test_e2e, "build_images", lambda **kwargs: None)
    monkeypatch.setattr(
        test_e2e, "run_test", lambda ctx, mode, test, persistent: steps.append("test")
    )
    monkeypatch.setattr(
        test_e2e,
        "put_back",
        lambda ctx, found, expected, suite_project: steps.append("put_back"),
    )
    for key in ("COMPOSE_PROJECT_NAME", "DFE_CONTAINER_PREFIX"):
        monkeypatch.delenv(key, raising=False)

    test_e2e.run_suite(
        test_e2e.TestContext(), "ci", [_Test()], persistent_services=["clickhouse"]
    )

    leftover = [
        "docker",
        "compose",
        "--project-name",
        "dfe-e2e",
        "--profile",
        "*",
        "down",
        "--volumes",
        "--remove-orphans",
    ]
    assert leftover in steps
    assert steps.index(leftover) < steps.index("test") < steps.index("put_back")


def test_isolation_keeps_a_rotated_password_out_of_dotenv_until_it_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(test_e2e, "SUITE_ENV_DIR", tmp_path / "suite-env")
    monkeypatch.setattr(test_e2e.post, "RECORD_ROTATION", True)
    for key in (
        "COMPOSE_PROJECT_NAME",
        "DFE_CONTAINER_PREFIX",
        "DFE_NETWORK_SUBNET",
        "DFE_PROXY_IP",
        "DFE_NETWORK_IP_RANGE",
    ):
        monkeypatch.delenv(key, raising=False)

    test_e2e.isolate_suite("dfe")
    isolated = test_e2e.post.RECORD_ROTATION
    test_e2e.end_isolation()

    assert isolated is False
    assert test_e2e.post.RECORD_ROTATION is True


class _Test:
    """The fields run_suite reads off a test case."""

    services: dict[str, str] = {}
    sources: tuple = ()
