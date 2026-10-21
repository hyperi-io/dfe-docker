#  Project:      dfe-docker
#  File:         tests/test_per_source_apps.py
#  Purpose:      Assert how a profile's per-source app keys are validated, resolved and enforced on the engine's instance index
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A profile runs an app once per source by setting that app's `*_enabled` key, never by naming the app as a service.

Each instance reads the config dfe-engine writes for its source, so a profile with the core footprint off runs none. An index naming an instance of an app the profile does not run fails the resolve.
"""

from typing import TypedDict

import pytest

import instances
import resolve_profile
from _common import PER_SOURCE_APP_KEYS, PER_SOURCE_APPS_VAR

ELASTIC = "dfe-transform-elastic"
FETCHER = "dfe-fetcher"
VECTOR = "dfe-transform-vector"
VRL = "dfe-transform-vrl"


class InstanceServicesCase(TypedDict):
    """The apps a profile runs, the index the engine wrote, whether the tier renders and the services declared."""

    enabled: list[str]
    expected: list[str]
    indexes: dict[str, list[str]]
    renders: bool


class PerSourceAppsCase(TypedDict):
    """The keys a profile declares, the tier and transport it runs and the apps it resolves to."""

    declared: dict[str, str]
    expected: list[str]
    renders: bool
    transport: str


class PerSourceAppsRefusalCase(TypedDict):
    """The keys a profile declares that the resolver refuses, its transport and what the refusal says."""

    declared: dict[str, object]
    message: str
    transport: str


class ResolvedExportCase(TypedDict):
    """One committed profile and the apps it writes to .tmp/profile.mk."""

    expected: str
    profile: str


INSTANCE_SERVICES_CASES = [
    InstanceServicesCase(
        enabled=[FETCHER],
        expected=[f"{FETCHER}-aws-cloudtrail", f"{FETCHER}-aws-guardduty"],
        indexes={FETCHER: ["aws-cloudtrail", "aws-guardduty"]},
        renders=True,
    ),
    InstanceServicesCase(enabled=[FETCHER, VRL], expected=[], indexes={}, renders=True),
    InstanceServicesCase(
        enabled=[FETCHER],
        expected=[f"{FETCHER}-okta-audit"],
        indexes={ELASTIC: [], FETCHER: ["okta-audit"]},
        renders=True,
    ),
    InstanceServicesCase(
        enabled=[], expected=[], indexes={FETCHER: ["okta-audit"]}, renders=False
    ),
]
PER_SOURCE_APPS_CASES = [
    PerSourceAppsCase(declared={}, expected=[], renders=True, transport="kafka"),
    PerSourceAppsCase(
        declared={"fetcher_enabled": "false"},
        expected=[],
        renders=True,
        transport="grpc",
    ),
    PerSourceAppsCase(
        declared={"fetcher_enabled": "True", "transform_vrl_enabled": "true"},
        expected=[FETCHER, VRL],
        renders=True,
        transport="grpc",
    ),
    PerSourceAppsCase(
        declared={
            "fetcher_enabled": "true",
            "transform_elastic_enabled": "true",
            "transform_vector_enabled": "true",
            "transform_vrl_enabled": "true",
        },
        expected=[FETCHER, ELASTIC, VECTOR, VRL],
        renders=True,
        transport="kafka",
    ),
    PerSourceAppsCase(
        declared={"fetcher_enabled": "true", "transform_vector_enabled": "true"},
        expected=[],
        renders=False,
        transport="kafka",
    ),
]
PER_SOURCE_APPS_REFUSAL_CASES = [
    PerSourceAppsRefusalCase(
        declared={"fetcher_enabled": "yes"},
        message="'fetcher_enabled' must be true or false",
        transport="kafka",
    ),
    PerSourceAppsRefusalCase(
        declared={"transform_vrl_enabled": {"enabled": "true"}},
        message="'transform_vrl_enabled' must be true or false",
        transport="kafka",
    ),
    PerSourceAppsRefusalCase(
        declared={"transform_elastic_enabled": "true"},
        message="dfe-transform-elastic runs on the kafka transport only",
        transport="grpc",
    ),
]
RESOLVED_EXPORT_CASES = [
    ResolvedExportCase(expected="", profile="slim-grpc"),
    ResolvedExportCase(expected="", profile="slim-kafka"),
    ResolvedExportCase(expected=f"{FETCHER} {VECTOR} {VRL}", profile="single-grpc"),
    ResolvedExportCase(
        expected=f"{FETCHER} {ELASTIC} {VECTOR} {VRL}", profile="single-kafka"
    ),
]


def _index(*, app: str, names: list[str]) -> None:
    """Write one app's instance index where the resolver reads it, as dfe-engine does."""
    path = instances.index_file(app)
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")


def _no_dotenv() -> None:
    """Stand in for resolve_profile's .env load, so the developer's .env changes nothing here."""


def _resolve(*, monkeypatch: pytest.MonkeyPatch, profile: str) -> int:
    """Run the resolver for one committed profile, with no .env and the core footprint on."""
    monkeypatch.setattr(resolve_profile, "_load_dotenv", _no_dotenv)
    monkeypatch.delenv("DFE_CORE_ENABLED", raising=False)
    monkeypatch.setenv("DFE_PROFILE", profile)
    return resolve_profile.main()


class TestInstanceServices:
    @pytest.mark.parametrize("case", INSTANCE_SERVICES_CASES)
    def test_matches_expected(self, case: InstanceServicesCase) -> None:
        for app, names in case["indexes"].items():
            _index(app=app, names=names)

        services = resolve_profile._instance_services(
            enabled=case["enabled"],
            local=[],
            profile="single-kafka",
            renders=case["renders"],
            transport="kafka",
        )

        assert services == case["expected"]

    @pytest.mark.parametrize(("app", "key"), sorted(PER_SOURCE_APP_KEYS.items()))
    def test_refuses_an_instance_of_an_app_the_profile_does_not_run(
        self, app: str, key: str
    ) -> None:
        _index(app=app, names=["okta-audit", "okta-system"])
        enabled = [other for other in PER_SOURCE_APP_KEYS if other != app]

        with pytest.raises(resolve_profile._ProfileError) as refused:
            resolve_profile._instance_services(
                enabled=enabled,
                local=[],
                profile="single-kafka",
                renders=True,
                transport="kafka",
            )

        assert f"- {app} (sources okta-audit, okta-system): set {key}: true" in (
            refused.value.msg
        )
        assert not (instances.COMPOSE_INSTANCES_FILE.exists())

    def test_refuses_a_kafka_only_instance_on_grpc_without_naming_its_key(
        self,
    ) -> None:
        _index(app=ELASTIC, names=["cisco-asa"])

        with pytest.raises(resolve_profile._ProfileError) as refused:
            resolve_profile._instance_services(
                enabled=[FETCHER, VECTOR, VRL],
                local=[],
                profile="single-grpc",
                renders=True,
                transport="grpc",
            )

        assert (
            f"- {ELASTIC} (sources cisco-asa): it runs on the kafka transport only"
            in refused.value.msg
        )

    def test_an_instance_of_a_local_app_runs_its_local_image(self) -> None:
        _index(app=FETCHER, names=["okta-audit"])
        _index(app=VRL, names=["cisco-ios"])

        resolve_profile._instance_services(
            enabled=[FETCHER, VRL],
            local=["dfe-engine", VRL],
            profile="single-kafka",
            renders=True,
            transport="kafka",
        )

        text = instances.COMPOSE_INSTANCES_FILE.read_text(encoding="utf-8")
        assert f"      service: {VRL}\n    image: {VRL}:local\n" in text
        assert text.count("    image: ") == 1


class TestPerSourceApps:
    @pytest.mark.parametrize("case", PER_SOURCE_APPS_CASES)
    def test_matches_expected(self, case: PerSourceAppsCase) -> None:
        apps = resolve_profile._per_source_apps(
            profile={"transport": case["transport"], **case["declared"]},
            profile_name=f"single-{case['transport']}",
            renders=case["renders"],
            transport=case["transport"],
        )

        assert apps == case["expected"]

    @pytest.mark.parametrize("case", PER_SOURCE_APPS_REFUSAL_CASES)
    def test_refuses(self, case: PerSourceAppsRefusalCase) -> None:
        with pytest.raises(resolve_profile._ProfileError, match=case["message"]):
            resolve_profile._per_source_apps(
                profile={"transport": case["transport"], **case["declared"]},
                profile_name=f"single-{case['transport']}",
                renders=True,
                transport=case["transport"],
            )


class TestResolveProfile:
    @pytest.mark.parametrize("case", RESOLVED_EXPORT_CASES)
    def test_exports_the_apps_each_profile_runs(
        self, monkeypatch: pytest.MonkeyPatch, case: ResolvedExportCase
    ) -> None:
        assert _resolve(monkeypatch=monkeypatch, profile=case["profile"]) == 0

        values = {}
        for line in resolve_profile.PROFILE_MK.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":=")
            values[name.replace("export", "").strip()] = value.strip()
        assert values[PER_SOURCE_APPS_VAR] == case["expected"]

    def test_fails_on_an_index_naming_an_app_the_profile_does_not_run(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _index(app=FETCHER, names=["aws-cloudtrail"])

        assert _resolve(monkeypatch=monkeypatch, profile="slim-grpc") == 1

        error = capsys.readouterr().err
        assert "(slim-grpc): dfe-engine declared instances of apps" in error
        assert "Set the key on the profile or delete those sources" in error
        assert f"- {FETCHER} (sources aws-cloudtrail): set fetcher_enabled: true" in (
            error
        )
        assert not (resolve_profile.PROFILE_MK.exists())

    @pytest.mark.parametrize(("app", "key"), sorted(PER_SOURCE_APP_KEYS.items()))
    def test_refuses_a_per_source_app_as_a_service(
        self, monkeypatch: pytest.MonkeyPatch, app: str, key: str
    ) -> None:
        monkeypatch.setenv("DFE_PROFILE", "single-kafka")
        data = {
            "profiles": {
                "single-kafka": {
                    "services": {app: {"config_path": "loader/kafka.yaml"}},
                    "transport": "kafka",
                }
            }
        }

        with pytest.raises(resolve_profile._ProfileError, match=f"Set {key}: true"):
            resolve_profile._resolve_profile(data=data)
