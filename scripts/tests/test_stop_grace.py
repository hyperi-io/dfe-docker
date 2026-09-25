#  Project:      dfe-docker
#  File:         tests/test_stop_grace.py
#  Purpose:      Assert the stop-grace guard reads compose durations and names the right services
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The stop-grace half of check_compose, on the resolved model compose prints.

A data-plane app holding source acknowledgements drains on SIGTERM, so a grace
under its figure SIGKILLs it mid-drain. The guard runs over `docker compose
config --format json`, which is a dict here.
"""

import pytest

import check_compose


@pytest.mark.parametrize(
    "text,seconds",
    [("45s", 45.0), ("1m10s", 70.0), ("1h", 3600.0), ("500ms", 0.5), ("1m", 60.0)],
)
def test_a_compose_duration_reads_in_seconds(text: str, seconds: float) -> None:
    assert check_compose._duration_seconds(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "45", "soon", "45s later", "s45"])
def test_a_duration_compose_would_not_print_reads_as_none(text: str) -> None:
    assert check_compose._duration_seconds(text) is None


@pytest.mark.parametrize(
    "service,app",
    [
        ("dfe-loader", "dfe-loader"),
        ("dfe-loader-tenant-a", "dfe-loader"),
        ("dfe-transform-vector-filebeat", "dfe-transform-vector"),
        ("dfe-transform-elastic-cisco-ios", "dfe-transform-elastic"),
        ("contract-dfe-loader", None),
        ("dfe-engine", None),
        ("dfe-hunt-runner", None),
    ],
)
def test_a_service_is_matched_to_the_app_it_runs(service: str, app: str | None) -> None:
    assert check_compose._app_of(service) == app


def test_every_app_is_held_to_its_own_figure() -> None:
    config = {
        "services": {
            "dfe-receiver": {"stop_grace_period": "45s"},
            "dfe-transform-vector": {"stop_grace_period": "1m30s"},
            "dfe-transform-vector-filebeat": {"stop_grace_period": "45s"},
            "dfe-loader": {},
            "contract-dfe-loader": {},
            "dfe-engine": {},
        }
    }

    failures = check_compose._stop_grace_failures(config=config)

    assert len(failures) == 2
    assert failures[0].startswith("dfe-loader: stop_grace_period is unset")
    assert failures[1].startswith(
        "dfe-transform-vector-filebeat: stop_grace_period is 45s"
    )
    assert "90s" in failures[1]


def test_transform_vector_under_ninety_seconds_is_caught() -> None:
    config = {"services": {"dfe-transform-vector": {"stop_grace_period": "70s"}}}

    failures = check_compose._stop_grace_failures(config=config)

    assert failures == [
        "dfe-transform-vector: stop_grace_period is 70s, under the 90s its drain "
        "of held acknowledgements needs"
    ]
