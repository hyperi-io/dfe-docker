#  Project:      dfe-docker
#  File:         scripts/tests/test_quickstart_json.py
#  Purpose:      Assert the quickstart.json a release carries reads every value from the files it names
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on scripts/quickstart_json.py, which need make and nothing else."""

import shutil

import pytest

import quickstart_json
import resolve_profile
from _common import SERVICE_PROFILES_FILE


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        pytest.param(["include:\n  - a.yml\n"], "2.20.0", id="include"),
        pytest.param(["ports: !reset []\n", "include:\n"], "2.20.0", id="newest-wins"),
        pytest.param(
            ["    env_file:\n      - path: env/a.env\n        required: false\n"],
            "2.24.0",
            id="env-file",
        ),
        pytest.param(
            ["        volume:\n          subpath: a\n"], "2.26.0", id="subpath"
        ),
    ],
)
def test_min_compose_is_the_newest_feature_used(
    expected: str, texts: list[str]
) -> None:
    assert quickstart_json.min_compose(texts=texts) == expected


def test_compose_text_with_no_known_feature_is_refused() -> None:
    with pytest.raises(quickstart_json.QuickstartJsonError):
        quickstart_json.min_compose(texts=["services: {}\n"])


def test_the_console_url_is_the_one_next_auth_is_handed() -> None:
    text = "      NEXTAUTH_URL: ${DFE_EXTERNAL_ORIGIN:-http://localhost}:${DFE_UI_PORT:-3000}\n"

    assert quickstart_json.console_url(compose_text=text) == "http://localhost:3000"


def test_a_release_document_from_the_committed_files() -> None:
    if shutil.which("make") is None:
        pytest.skip("the default profile is read through make")
    profiles = resolve_profile.parse_yaml(
        text=SERVICE_PROFILES_FILE.read_text(encoding="utf-8")
    )["profiles"]

    document = quickstart_json.quickstart(
        default=quickstart_json.default_profile(),
        image="ghcr.io/hyperi-io/dfe-docker",
        profiles=profiles,
        stack_version="2.2.2",
        version="9.9.9",
    )

    assert document["schema"] == 1
    assert document["kind"] == "image"
    assert [entry["name"] for entry in document["profiles"]] == list(profiles)
    assert sum(entry["default"] for entry in document["profiles"]) == 1
    assert {entry["transport"] for entry in document["profiles"]} <= {"grpc", "kafka"}
    for key in ("up", "demo"):
        assert document[key]["pinned"][0].endswith("dfe-docker:9.9.9 init")
        assert document[key]["latest"][0].endswith("dfe-docker:latest init")
        assert all(line.startswith("docker ") for line in document[key]["latest"])


def test_an_unknown_default_profile_is_refused() -> None:
    with pytest.raises(quickstart_json.QuickstartJsonError, match="default profile"):
        quickstart_json.quickstart(
            default="single",
            image="ghcr.io/hyperi-io/dfe-docker",
            profiles={"slim-grpc": {"transport": "grpc"}},
            stack_version="2.2.2",
            version="9.9.9",
        )
