#  Project:      dfe-docker
#  File:         tests/test_quickstart.py
#  Purpose:      Assert how `make up` and `make demo` choose the profile and stack, and what they refuse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on the one-command quickstart that need no stack: the profile and stack choice, the demo's refusal, and the .env line it writes."""

import re

import pytest

import init
import quickstart
import resolve_profile
from _common import REPO_ROOT, SERVICE_PROFILES_FILE
from _sources import enabled_apps


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        pytest.param(
            {"explicit": "slim-grpc", "environ": "slim-kafka", "dotenv": "single-grpc"},
            ("slim-grpc", "make line"),
            id="make_line_wins",
        ),
        pytest.param(
            {"explicit": "", "environ": "slim-kafka", "dotenv": "single-grpc"},
            ("single-grpc", ".env"),
            id="dotenv_beats_environment_as_make_applies_it",
        ),
        pytest.param(
            {"explicit": "", "environ": "slim-kafka", "dotenv": ""},
            ("slim-kafka", "environment"),
            id="environment_when_dotenv_is_silent",
        ),
        pytest.param(
            {"explicit": " ", "environ": "", "dotenv": "", "pinned": False},
            ("single-kafka", "default"),
            id="bare_clone_takes_the_default",
        ),
        pytest.param(
            {"explicit": "", "environ": "", "dotenv": "", "pinned": True},
            ("slim-grpc", "active_profile"),
            id="existing_deployment_keeps_the_active_profile",
        ),
    ],
)
def test_chooses_the_profile_nearest_the_user(given: dict, expected: tuple) -> None:
    assert (
        quickstart.choose_profile(
            active="slim-grpc", default="single-kafka", **({"pinned": False} | given)
        )
        == expected
    )


@pytest.mark.parametrize(
    ("dotenv", "explicit", "expected"),
    [
        pytest.param({}, "", ("latest", "default"), id="bare_clone_takes_the_newest"),
        pytest.param(
            {"DFE_STACK_VERSION": "2.2.1"},
            "",
            ("2.2.1", ".env"),
            id="recorded_stack_without_pins",
        ),
        pytest.param(
            {"DFE_ENGINE_VERSION": "2.2.2@sha256:x", "DFE_STACK_VERSION": "2.2.2"},
            "",
            ("", ".env"),
            id="pins_kept",
        ),
        pytest.param(
            {"DFE_ENGINE_VERSION": "2.2.2@sha256:x"},
            "2.2.1",
            ("2.2.1", "make line"),
            id="make_line_repins",
        ),
    ],
)
def test_chooses_the_stack_to_pin(dotenv: dict, explicit: str, expected: tuple) -> None:
    assert quickstart.choose_stack(dotenv=dotenv, explicit=explicit) == expected


def _apps(name: str) -> list[str]:
    return enabled_apps(
        profile_name=name,
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8"),
    )


@pytest.mark.parametrize(
    ("name", "running", "message"),
    [
        pytest.param("single-kafka", "", "", id="single_kafka_runs_the_demo"),
        pytest.param("single-grpc", "", "", id="single_grpc_runs_the_demo"),
        pytest.param(
            "slim-grpc",
            "",
            "profile slim-grpc runs no dfe-transform-elastic.*PROFILE=single-kafka",
            id="slim_is_refused_by_name",
        ),
        pytest.param(
            "slim-kafka",
            "slim-kafka",
            "stack running here is profile slim-kafka.*make down",
            id="running_slim_stack_is_not_switched",
        ),
    ],
)
def test_the_demo_refuses_a_profile_without_the_transform(
    name: str, running: str, message: str
) -> None:
    reason = quickstart.refusal(apps=_apps(name), name=name, running=running)

    assert re.search(message, reason) if message else reason == "", reason


def test_the_default_and_the_active_profile_are_committed_profiles() -> None:
    parsed = resolve_profile.parse_yaml(
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
    )
    default = re.search(
        r"(?m)^DEFAULT_PROFILE := (\S+)$", (REPO_ROOT / "Makefile").read_text()
    )

    assert default and default.group(1) in parsed["profiles"]
    assert parsed["active_profile"] in parsed["profiles"]


def test_the_profile_line_is_replaced_not_appended() -> None:
    text = "A=1\n# DFE_PROFILE=\nDFE_PROFILE=slim-kafka\nB=2\n"

    out = init._set_live(text=text, key="DFE_PROFILE", value="single-kafka")

    assert out.splitlines().count("DFE_PROFILE=single-kafka") == 1
    assert "slim-kafka" not in out
    assert out.startswith("A=1\n") and out.rstrip().endswith("B=2")
