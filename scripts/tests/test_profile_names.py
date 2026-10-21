#  Project:      dfe-docker
#  File:         tests/test_profile_names.py
#  Purpose:      Assert which profile names resolve and the tier each one reports to dfe-engine
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Every profile is `<tier>-<transport>` and dfe-engine is told the tier as `docker-<tier>`.

A retired name is refused with the profile that replaces it, never resolved as an alias.
"""

from pathlib import Path
from typing import TypedDict

import pytest

import resolve_profile
from _common import RENDERED_PROFILES, SERVICE_PROFILES_FILE


class ResolveProfileRefusalCase(TypedDict):
    """One profile name, the transport its entry declares and what the refusal says."""

    message: str
    name: str
    transport: str


class StackProfileCase(TypedDict):
    """One profile and the tier dfe-engine is told it runs."""

    expected: str
    profile: str


RESOLVE_PROFILE_REFUSAL_CASES = [
    ResolveProfileRefusalCase(
        message="to 'slim-grpc' instead", name="slim", transport="grpc"
    ),
    ResolveProfileRefusalCase(
        message="to 'single-kafka' instead", name="single", transport="kafka"
    ),
    ResolveProfileRefusalCase(
        message="to 'slim-kafka' instead", name="kafka-loader", transport="kafka"
    ),
    ResolveProfileRefusalCase(
        message="to 'single-grpc' instead",
        name="grpc-loader-archiver",
        transport="grpc",
    ),
    ResolveProfileRefusalCase(
        message="Available profiles", name="scale", transport="kafka"
    ),
    ResolveProfileRefusalCase(
        message="which says 'grpc'", name="slim-grpc", transport="kafka"
    ),
]
STACK_PROFILE_CASES = [
    StackProfileCase(expected="docker-slim", profile="slim-grpc"),
    StackProfileCase(expected="docker-slim", profile="slim-kafka"),
    StackProfileCase(expected="docker-single", profile="single-grpc"),
    StackProfileCase(expected="docker-single", profile="single-kafka"),
]


def _no_dotenv() -> None:
    """Stand in for resolve_profile's .env load, so the developer's .env changes nothing here."""


def _profile_mk(
    *, monkeypatch: pytest.MonkeyPatch, profile: str, tmp_path: Path
) -> dict[str, str]:
    """Run the resolver for one committed profile and read back the assignments it wrote."""
    written = tmp_path / ".tmp" / "profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    monkeypatch.setattr(resolve_profile, "_load_dotenv", _no_dotenv)
    monkeypatch.setenv("DFE_PROFILE", profile)
    assert resolve_profile.main() == 0
    values = {}
    for line in written.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


class TestResolveProfile:
    @pytest.mark.parametrize("case", RESOLVE_PROFILE_REFUSAL_CASES)
    def test_refuses(
        self, monkeypatch: pytest.MonkeyPatch, case: ResolveProfileRefusalCase
    ) -> None:
        monkeypatch.setenv("DFE_PROFILE", case["name"])
        loader = {"config_path": f"loader/{case['transport']}.yaml"}
        data = {
            "profiles": {
                case["name"]: {
                    "services": {"dfe-loader": loader},
                    "transport": case["transport"],
                }
            }
        }

        with pytest.raises(resolve_profile._ProfileError, match=case["message"]):
            resolve_profile._resolve_profile(data=data)


class TestServiceProfiles:
    def test_defines_every_rendered_profile_and_no_other(self) -> None:
        data = resolve_profile._parse_yaml(
            text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
        )

        assert sorted(data["profiles"]) == sorted(RENDERED_PROFILES)


class TestStackProfile:
    @pytest.mark.parametrize("case", STACK_PROFILE_CASES)
    def test_matches_expected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        case: StackProfileCase,
    ) -> None:
        values = _profile_mk(
            monkeypatch=monkeypatch, profile=case["profile"], tmp_path=tmp_path
        )

        assert values["DFE_STACK_PROFILE"] == case["expected"]
