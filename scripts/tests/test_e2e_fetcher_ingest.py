#  Project:      dfe-docker
#  File:         tests/test_e2e_fetcher_ingest.py
#  Purpose:      Assert every e2e test that posts to dfe-fetcher runs it with ingest on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The e2e fetcher tests post to an ingest endpoint their config turns on.

dfe-fetcher leaves ingest off unless configured, and a test on a config that
leaves it off gets no answer to any POST. The stack configs keep it off, so
`make dev` publishes no unauthenticated ingest. A text read, because these
tests run with no PyYAML.
"""

from pathlib import Path

import resolve_profile
from _common import CONFIG_DIR, REPO_ROOT, SERVICE_PROFILES_FILE

_E2E_TESTS = REPO_ROOT / "tests" / "e2e" / "e2e-tests.yaml"


def _ingest_enabled(path: Path) -> bool:
    """Return whether a fetcher config sets `ingest.enabled: true`."""
    in_ingest = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not (line) or line.startswith("#"):
            continue
        if not (raw.startswith((" ", "\t"))):
            in_ingest = line == "ingest:"
        elif in_ingest and line == "enabled: true":
            return True
    return False


def _e2e_tests() -> dict[str, dict[str, str]]:
    """Return each e2e test's profile and any dfe-fetcher override, by name."""
    tests: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in _E2E_TESTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("- name: "):
            current = tests.setdefault(line.split(": ", 1)[1], {})
        elif current is not None and line.startswith(("profile: ", "dfe-fetcher: ")):
            key, value = line.split(": ", 1)
            current[key] = value
    return tests


def _fetcher_ingest_profiles() -> dict[str, str]:
    """Return {profile: fetcher config path} for each profile the suite posts to the fetcher.

    test_e2e.py sends to the fetcher's ingest URL when the profile has a fetcher
    and no receiver.
    """
    profiles = resolve_profile._parse_yaml(
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
    )["profiles"]
    return {
        name: f"config/{profile['services']['dfe-fetcher']['config_path']}"
        for name, profile in profiles.items()
        if "dfe-fetcher" in profile["services"]
        and "dfe-receiver" not in profile["services"]
    }


def test_the_config_reader_sees_ingest_on_and_off(tmp_path: Path) -> None:
    on = tmp_path / "on.yaml"
    on.write_text("output:\n  grpc: {}\ningest:\n  enabled: true\n", encoding="utf-8")
    off = tmp_path / "off.yaml"
    off.write_text(
        "output:\n  enabled: true\ningest:\n  enabled: false\n", encoding="utf-8"
    )

    assert _ingest_enabled(on)
    assert not _ingest_enabled(off)


def test_every_fetcher_ingest_test_runs_a_config_with_ingest_on() -> None:
    profiles = _fetcher_ingest_profiles()
    tests = {
        name: test for name, test in _e2e_tests().items() if test["profile"] in profiles
    }

    off = {}
    for name, test in tests.items():
        path = test.get("dfe-fetcher", profiles[test["profile"]])
        if not (_ingest_enabled(REPO_ROOT / path)):
            off[name] = path

    assert tests
    assert off == {}, "ingest is off in the fetcher config these tests post to"


def test_no_stack_fetcher_config_turns_ingest_on() -> None:
    on = sorted(
        path.name
        for path in (CONFIG_DIR / "fetcher").glob("*.yaml")
        if _ingest_enabled(path)
    )

    assert on == []
