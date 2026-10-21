#  Project:      dfe-docker
#  File:         tests/test_fetcher_enabled.py
#  Purpose:      Assert how a profile's fetcher_enabled key is validated and resolved
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A profile runs dfe-fetcher once per source by declaring `fetcher_enabled: true`, never by naming the fetcher as a service.

Each instance reads the config dfe-engine writes for its source, so a profile with the core footprint off runs none.
"""

from pathlib import Path
from typing import TypedDict

import pytest

import resolve_profile
from _common import FETCHER_ENABLED_KEY, FETCHER_RESOLVED_VAR


class FetcherEnabledCase(TypedDict):
    """One declared value, the tier it is declared on and what it resolves to."""

    declared: str | None
    expected: bool
    profile_name: str
    renders: bool


class FetcherEnabledRefusalCase(TypedDict):
    """One declared value the resolver refuses, the tier it is declared on and what the refusal says."""

    declared: object
    message: str
    profile_name: str


class ResolvedExportCase(TypedDict):
    """One committed profile and the value it writes to .tmp/profile.mk."""

    expected: str
    profile: str


FETCHER_ENABLED_CASES = [
    FetcherEnabledCase(
        declared=None, expected=False, profile_name="slim-kafka", renders=False
    ),
    FetcherEnabledCase(
        declared="false", expected=False, profile_name="slim-kafka", renders=False
    ),
    FetcherEnabledCase(
        declared="true", expected=True, profile_name="single-kafka", renders=True
    ),
    FetcherEnabledCase(
        declared="True", expected=True, profile_name="single-kafka", renders=True
    ),
    FetcherEnabledCase(
        declared="true", expected=False, profile_name="single-kafka", renders=False
    ),
    FetcherEnabledCase(
        declared="false", expected=False, profile_name="single-kafka", renders=True
    ),
]
FETCHER_ENABLED_REFUSAL_CASES = [
    FetcherEnabledRefusalCase(
        declared="yes", message="must be true or false", profile_name="single-kafka"
    ),
    FetcherEnabledRefusalCase(
        declared={"enabled": "true"},
        message="must be true or false",
        profile_name="single-kafka",
    ),
]
RESOLVED_EXPORT_CASES = [
    ResolvedExportCase(expected="false", profile="slim-grpc"),
    ResolvedExportCase(expected="false", profile="slim-kafka"),
    ResolvedExportCase(expected="true", profile="single-grpc"),
    ResolvedExportCase(expected="true", profile="single-kafka"),
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
    monkeypatch.delenv("DFE_CORE_ENABLED", raising=False)
    monkeypatch.setenv("DFE_PROFILE", profile)
    assert resolve_profile.main() == 0
    values = {}
    for line in written.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


class TestFetcherEnabled:
    @pytest.mark.parametrize("case", FETCHER_ENABLED_CASES)
    def test_matches_expected(self, case: FetcherEnabledCase) -> None:
        profile = {"transport": "kafka"}
        if case["declared"] is not None:
            profile[FETCHER_ENABLED_KEY] = case["declared"]

        enabled = resolve_profile._fetcher_enabled(
            profile=profile,
            profile_name=case["profile_name"],
            renders=case["renders"],
        )

        assert enabled is case["expected"]

    @pytest.mark.parametrize("case", FETCHER_ENABLED_REFUSAL_CASES)
    def test_refuses(self, case: FetcherEnabledRefusalCase) -> None:
        profile = {FETCHER_ENABLED_KEY: case["declared"], "transport": "kafka"}

        with pytest.raises(resolve_profile._ProfileError, match=case["message"]):
            resolve_profile._fetcher_enabled(
                profile=profile, profile_name=case["profile_name"], renders=True
            )


class TestResolveProfile:
    @pytest.mark.parametrize("case", RESOLVED_EXPORT_CASES)
    def test_exports_whether_the_profile_runs_fetchers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        case: ResolvedExportCase,
    ) -> None:
        values = _profile_mk(
            monkeypatch=monkeypatch, profile=case["profile"], tmp_path=tmp_path
        )

        assert values[FETCHER_RESOLVED_VAR] == case["expected"]

    def test_refuses_dfe_fetcher_as_a_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DFE_PROFILE", "single-kafka")
        data = {
            "profiles": {
                "single-kafka": {
                    "services": {"dfe-fetcher": {"config_path": "loader/kafka.yaml"}},
                    "transport": "kafka",
                }
            }
        }

        with pytest.raises(
            resolve_profile._ProfileError, match=f"Set {FETCHER_ENABLED_KEY}: true"
        ):
            resolve_profile._resolve_profile(data=data)
