#  Project:      dfe-docker
#  File:         tests/test_quickstart.py
#  Purpose:      Assert how `make up` and `make demo` choose the profile and stack and what they refuse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on the one-command quickstart that need no stack: the profile and stack choice, the demo's refusal and the .env line it writes."""

import argparse
import re
from typing import TypedDict

import pytest

import init
import quickstart
import resolve_profile
from _common import REPO_ROOT, SERVICE_PROFILES_FILE
from _sources import enabled_apps


class ChooseProfileCase(TypedDict):
    """Each place a profile can be set and the profile and source that wins."""

    dotenv: str
    environ: str
    expected: tuple[str, str]
    explicit: str
    pinned: bool


class ChooseStackCase(TypedDict):
    """A .env and a make line and the stack and source that wins."""

    dotenv: dict[str, str]
    expected: tuple[str, str]
    explicit: str


class RefusalCase(TypedDict):
    """A profile, the stack running and the pattern its refusal matches, empty when the demo can run."""

    message: str
    name: str
    running: str


CHOOSE_PROFILE_CASES = [
    ChooseProfileCase(
        dotenv="single-grpc",
        environ="slim-kafka",
        expected=("slim-grpc", "make line"),
        explicit="slim-grpc",
        pinned=False,
    ),
    ChooseProfileCase(
        dotenv="single-grpc",
        environ="slim-kafka",
        expected=("single-grpc", ".env"),
        explicit="",
        pinned=False,
    ),
    ChooseProfileCase(
        dotenv="",
        environ="slim-kafka",
        expected=("slim-kafka", "environment"),
        explicit="",
        pinned=False,
    ),
    ChooseProfileCase(
        dotenv="",
        environ="",
        expected=("single-kafka", "default"),
        explicit=" ",
        pinned=False,
    ),
    ChooseProfileCase(
        dotenv="",
        environ="",
        expected=("slim-grpc", "active_profile"),
        explicit="",
        pinned=True,
    ),
]
CHOOSE_STACK_CASES = [
    ChooseStackCase(dotenv={}, expected=("latest", "default"), explicit=""),
    ChooseStackCase(
        dotenv={"DFE_STACK_VERSION": "2.2.1"}, expected=("2.2.1", ".env"), explicit=""
    ),
    ChooseStackCase(
        dotenv={"DFE_ENGINE_VERSION": "2.2.2@sha256:x", "DFE_STACK_VERSION": "2.2.2"},
        expected=("", ".env"),
        explicit="",
    ),
    ChooseStackCase(
        dotenv={"DFE_ENGINE_VERSION": "2.2.2@sha256:x"},
        expected=("2.2.1", "make line"),
        explicit="2.2.1",
    ),
]
REFUSAL_CASES = [
    RefusalCase(message="", name="single-kafka", running=""),
    RefusalCase(message="", name="single-grpc", running=""),
    RefusalCase(
        message="profile 'slim-grpc' runs no 'dfe-transform-elastic'.*PROFILE=single-kafka",
        name="slim-grpc",
        running="",
    ),
    RefusalCase(
        message="stack running here is profile 'slim-kafka'.*make down",
        name="slim-kafka",
        running="slim-kafka",
    ),
]


def _apps(*, name: str) -> list[str]:
    """The apps a committed profile enables."""
    return enabled_apps(
        profile_name=name,
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8"),
    )


class TestChooseProfile:
    @pytest.mark.parametrize("case", CHOOSE_PROFILE_CASES)
    def test_matches_expected(self, case: ChooseProfileCase) -> None:
        chosen = quickstart._choose_profile(
            active="slim-grpc",
            default="single-kafka",
            dotenv=case["dotenv"],
            environ=case["environ"],
            explicit=case["explicit"],
            pinned=case["pinned"],
        )

        assert chosen == case["expected"]


class TestChooseStack:
    @pytest.mark.parametrize("case", CHOOSE_STACK_CASES)
    def test_matches_expected(self, case: ChooseStackCase) -> None:
        chosen = quickstart._choose_stack(
            dotenv=case["dotenv"], explicit=case["explicit"]
        )

        assert chosen == case["expected"]


class TestChoiceLine:
    @pytest.mark.parametrize(
        ("caller", "version_from", "stack_from", "said", "absent"),
        [
            pytest.param(
                "",
                "",
                "default",
                "make up: profile 'slim-grpc' (make line), stack '2.2.1' (default). Override with: make up PROFILE=",
                "--profile",
                id="make",
            ),
            pytest.param(
                "",
                "image default",
                "make line",
                "make up: profile 'slim-grpc' (make line), stack '2.2.1' (make line).",
                "image default",
                id="make-ignores-version-from",
            ),
            pytest.param(
                "init",
                "",
                "default",
                "init: profile 'slim-grpc' (given), stack '2.2.1' (default). Override with: init --profile",
                "make",
                id="image",
            ),
            pytest.param(
                "init",
                "given",
                "make line",
                "init: profile 'slim-grpc' (given), stack '2.2.1' (given).",
                "make",
                id="image-stack-given",
            ),
            pytest.param(
                "init",
                "image default",
                "make line",
                "init: profile 'slim-grpc' (given), stack '2.2.1' (image default).",
                "make",
                id="image-stack-baked",
            ),
        ],
    )
    def test_names_the_command_that_ran_it(
        self, absent: str, caller: str, said: str, stack_from: str, version_from: str
    ) -> None:
        args = argparse.Namespace(
            caller=caller, command="up", version_from=version_from
        )

        line = quickstart._choice_line(
            args=args,
            name="slim-grpc",
            profile_from="make line",
            stack_from=stack_from,
            version="2.2.1",
        )

        assert line.startswith(said)
        assert absent not in line


class TestRefusal:
    @pytest.mark.parametrize("case", REFUSAL_CASES)
    def test_matches_expected(self, case: RefusalCase) -> None:
        reason = quickstart._refusal(
            apps=_apps(name=case["name"]), name=case["name"], running=case["running"]
        )

        if case["message"]:
            assert re.search(pattern=case["message"], string=reason), reason
        else:
            assert reason == ""


class TestCommittedProfiles:
    def test_the_default_and_the_active_profile_are_committed_profiles(self) -> None:
        parsed = resolve_profile.parse_yaml(
            text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
        )
        default = re.search(
            pattern=r"(?m)^DEFAULT_PROFILE := (\S+)$",
            string=(REPO_ROOT / "Makefile").read_text(encoding="utf-8"),
        )

        assert default and default.group(1) in parsed["profiles"]
        assert parsed["active_profile"] in parsed["profiles"]


class TestSetLive:
    def test_the_profile_line_is_replaced_not_appended(self) -> None:
        text = "A=1\n# DFE_PROFILE=\nDFE_PROFILE=slim-kafka\nB=2\n"

        out = init._set_live(key="DFE_PROFILE", text=text, value="single-kafka")

        assert out.splitlines().count("DFE_PROFILE=single-kafka") == 1
        assert "slim-kafka" not in out
        assert out.startswith("A=1\n") and out.rstrip().endswith("B=2")
