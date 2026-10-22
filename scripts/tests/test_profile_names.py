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


class EngineTierCase(TypedDict):
    """The broker and content directory a tier is configured with, whether the engine renders there and what it is told."""

    configured_broker: str
    content_dir: str
    expected: dict[str, str]
    profile: str
    renders: bool


class ResolveProfileRefusalCase(TypedDict):
    """One profile name, the transport its entry declares and what the refusal says."""

    message: str
    name: str
    transport: str


class StackProfileCase(TypedDict):
    """One profile and the tier dfe-engine is told it runs."""

    expected: str
    profile: str


# What the engine is told on a tier it renders for, the broker and transport keys aside
_RENDERED_PATHS = {
    "DFE_APP_CONFIG_MOUNT": "/etc/dfe/apps",
    "DFE_ENGINE_APP_CONFIG_BASE_DIR": "/app/app-config-base",
    "DFE_ENGINE_APP_CONFIG_DIR": "/app/app-config",
}

ENGINE_TIER_CASES = [
    EngineTierCase(
        configured_broker="",
        content_dir="/app/content",
        expected={
            **_RENDERED_PATHS,
            "DFE_ENGINE_CATALOGUE_FILE": "/app/content/catalogue/sources.yaml",
            "DFE_ENGINE_CONTRACT_DIR": "/app/content/contract",
            "DFE_KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
            "DFE_STACK_PROFILE": "docker-single",
            "DFE_TRANSPORT_BUS_PRESENT": "true",
            "DFE_TRANSPORT_DEFAULT": "bus",
        },
        profile="single-kafka",
        renders=True,
    ),
    EngineTierCase(
        configured_broker=" broker.example.invalid:9092 ",
        content_dir="/srv/content",
        expected={
            **_RENDERED_PATHS,
            "DFE_ENGINE_CATALOGUE_FILE": "/srv/content/catalogue/sources.yaml",
            "DFE_ENGINE_CONTRACT_DIR": "/srv/content/contract",
            "DFE_KAFKA_BOOTSTRAP_SERVERS": "broker.example.invalid:9092",
            "DFE_STACK_PROFILE": "docker-slim",
            "DFE_TRANSPORT_BUS_PRESENT": "true",
            "DFE_TRANSPORT_DEFAULT": "bus",
        },
        profile="slim-kafka",
        renders=True,
    ),
    EngineTierCase(
        configured_broker="broker.example.invalid:9092",
        content_dir="/app/content",
        expected={
            "DFE_APP_CONFIG_MOUNT": "/etc/dfe/apps",
            "DFE_ENGINE_APP_CONFIG_BASE_DIR": "",
            "DFE_ENGINE_APP_CONFIG_DIR": "",
            "DFE_ENGINE_CATALOGUE_FILE": "",
            "DFE_ENGINE_CONTRACT_DIR": "",
            "DFE_KAFKA_BOOTSTRAP_SERVERS": "",
            "DFE_STACK_PROFILE": "docker-single",
            "DFE_TRANSPORT_BUS_PRESENT": "false",
            "DFE_TRANSPORT_DEFAULT": "direct",
        },
        profile="single-grpc",
        renders=False,
    ),
]
RESOLVE_PROFILE_REFUSAL_CASES = [
    ResolveProfileRefusalCase(
        message="to 'slim-grpc' instead", name="slim", transport="grpc"
    ),
    ResolveProfileRefusalCase(
        message="to 'single-kafka' instead", name="single", transport="kafka"
    ),
    ResolveProfileRefusalCase(
        message="to 'slim-kafka' instead", name="kafka-receiver", transport="kafka"
    ),
    ResolveProfileRefusalCase(
        message="to 'single-kafka' instead",
        name="kafka-full-transform-vrl",
        transport="kafka",
    ),
    ResolveProfileRefusalCase(
        message="to 'slim-grpc' instead", name="grpc-minimal", transport="grpc"
    ),
    ResolveProfileRefusalCase(
        message="to 'single-grpc' instead",
        name="grpc-receiver-transform-vrl",
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


def _profile_mk(
    *, monkeypatch: pytest.MonkeyPatch, profile: str, tmp_path: Path
) -> dict[str, str]:
    """Run the resolver for one committed profile and read back the assignments it wrote."""
    written = tmp_path / ".tmp" / "profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    monkeypatch.setenv("DFE_PROFILE", profile)
    assert resolve_profile.main() == 0
    values = {}
    for line in written.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


class TestEngineTier:
    @pytest.mark.parametrize("case", ENGINE_TIER_CASES)
    def test_matches_expected(self, case: EngineTierCase) -> None:
        tier = resolve_profile.engine_tier(
            configured_broker=case["configured_broker"],
            content_dir=case["content_dir"],
            profile_name=case["profile"],
            renders=case["renders"],
            transport=case["profile"].partition("-")[2],
        )

        assert tier == case["expected"]


class TestResolveProfile:
    @pytest.mark.parametrize(
        ("name", "replacement"), sorted(resolve_profile.RETIRED_PROFILES.items())
    )
    def test_a_retired_name_is_replaced_by_a_profile_on_its_own_transport(
        self, name: str, replacement: str
    ) -> None:
        transport = name.partition("-")[0]

        assert replacement in RENDERED_PROFILES
        if transport in resolve_profile.TRANSPORT_TYPES:
            assert replacement.endswith(f"-{transport}")

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

        with pytest.raises(resolve_profile.ProfileError, match=case["message"]):
            resolve_profile._resolve_profile(data=data)


class TestServiceProfiles:
    def test_defines_every_rendered_profile_and_no_other(self) -> None:
        data = resolve_profile.parse_yaml(
            text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
        )

        assert sorted(data["profiles"]) == sorted(RENDERED_PROFILES)


class TestStackProfile:
    # The resolver loads a throwaway .env, so the developer's own changes nothing here
    @pytest.mark.usefixtures("dotenv")
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
