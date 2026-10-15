#  Project:      dfe-docker
#  File:         tests/test_fetcher_enabled.py
#  Purpose:      Assert how a profile's fetcher_enabled key is projected, validated and resolved
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A profile runs dfe-fetcher once per source by declaring `fetcher_enabled: true`, never by naming the fetcher as a service.

Only a tier whose app config dfe-engine renders can run one, because each instance reads the config the engine writes for its source.
"""

from pathlib import Path
from typing import TypedDict

import pytest

import render_profiles
import resolve_profile
from _common import FETCHER_ENABLED_KEY, FETCHER_RESOLVED_VAR


class FetcherAsKeyCase(TypedDict):
    """One profile as dfe-infra renders it and as it is written into service_profiles.yaml."""

    expected: str
    rendered: str


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


_SINGLE_RENDERED = """\
  single:
    transport: kafka
    otel: true
    services:
      dfe-archiver:
        config_path: archiver/kafka.yaml
      dfe-fetcher:
        config_path: fetcher/kafka.yaml
      dfe-loader:
        config_path: loader/kafka.yaml
"""
_SINGLE_WRITTEN = """\
  single:
    transport: kafka
    otel: true
    fetcher_enabled: true
    services:
      dfe-archiver:
        config_path: archiver/kafka.yaml
      dfe-loader:
        config_path: loader/kafka.yaml
"""
_SLIM_RENDERED = """\
  slim:
    transport: grpc
    services:
      dfe-loader:
        config_path: loader/grpc.yaml
"""

FETCHER_AS_KEY_CASES = [
    FetcherAsKeyCase(expected=_SINGLE_WRITTEN, rendered=_SINGLE_RENDERED),
    FetcherAsKeyCase(expected=_SLIM_RENDERED, rendered=_SLIM_RENDERED),
    FetcherAsKeyCase(
        expected=(
            "  single:\n"
            "    transport: kafka\n"
            "    fetcher_enabled: true\n"
            "    services:\n"
            "      dfe-archiver:\n"
            "        config_path: archiver/kafka.yaml\n"
        ),
        rendered=(
            "  single:\n"
            "    transport: kafka\n"
            "    services:\n"
            "      dfe-archiver:\n"
            "        config_path: archiver/kafka.yaml\n"
            "      dfe-fetcher:\n"
            "        config_path: fetcher/kafka.yaml"
        ),
    ),
]
FETCHER_ENABLED_CASES = [
    FetcherEnabledCase(
        declared=None, expected=False, profile_name="kafka-receiver", renders=False
    ),
    FetcherEnabledCase(
        declared="false", expected=False, profile_name="kafka-receiver", renders=False
    ),
    FetcherEnabledCase(
        declared="true", expected=True, profile_name="single", renders=True
    ),
    FetcherEnabledCase(
        declared="True", expected=True, profile_name="single", renders=True
    ),
    FetcherEnabledCase(
        declared="true", expected=False, profile_name="single", renders=False
    ),
    FetcherEnabledCase(
        declared="false", expected=False, profile_name="single", renders=True
    ),
]
FETCHER_ENABLED_REFUSAL_CASES = [
    FetcherEnabledRefusalCase(
        declared="true",
        message="it does only on slim, single",
        profile_name="kafka-receiver",
    ),
    FetcherEnabledRefusalCase(
        declared="yes", message="must be true or false", profile_name="single"
    ),
    FetcherEnabledRefusalCase(
        declared={"enabled": "true"},
        message="must be true or false",
        profile_name="single",
    ),
]
RESOLVED_EXPORT_CASES = [
    ResolvedExportCase(expected="false", profile="kafka-receiver"),
    ResolvedExportCase(expected="true", profile="single"),
    ResolvedExportCase(expected="false", profile="slim"),
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


class TestFetcherAsKey:
    @pytest.mark.parametrize("case", FETCHER_AS_KEY_CASES)
    def test_matches_expected(self, case: FetcherAsKeyCase) -> None:
        written = render_profiles._fetcher_as_key(rendered=case["rendered"])

        assert written == case["expected"]


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


class TestProjectedBlock:
    def test_writes_the_key_where_the_tier_renders_a_fetcher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def render(infra_dir: Path, mode: str) -> str:
            return {"single": _SINGLE_RENDERED, "slim": _SLIM_RENDERED}[mode]

        monkeypatch.setattr(render_profiles, "_render", render)

        block = render_profiles.projected_block(infra_dir=Path("unused"))

        assert f"{FETCHER_ENABLED_KEY}: true" in block
        assert "dfe-fetcher" not in block
        assert _SLIM_RENDERED.rstrip("\n") in block


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
        monkeypatch.setenv("DFE_PROFILE", "fetching")
        data = {
            "profiles": {
                "fetching": {
                    "services": {"dfe-fetcher": {"config_path": "loader/kafka.yaml"}},
                    "transport": "kafka",
                }
            }
        }

        with pytest.raises(
            resolve_profile._ProfileError, match=f"Set {FETCHER_ENABLED_KEY}: true"
        ):
            resolve_profile._resolve_profile(data=data)
