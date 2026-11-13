#  Project:      dfe-docker
#  File:         scripts/tests/test_quickstart_json.py
#  Purpose:      Assert the quickstart.json a release carries reads every value from the files it names
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on scripts/quickstart_json.py, which need make and nothing else."""

import json
import shutil
from pathlib import Path

import pytest

import quickstart_json
import resolve_profile
from _common import FIRST_LOGIN_PASSWORD, SERVICE_PROFILES_FILE, demo_pack_names

IMAGE = "ghcr.io/hyperi-io/dfe-docker"


def _pack(root: Path, name: str, manifest: object) -> None:
    (root / name).mkdir()
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (root / name / "demo.json").write_text(text, encoding="utf-8")


def test_schema_2_lists_the_packs_in_directory_order(tmp_path: Path) -> None:
    _pack(tmp_path, "demo", {"sources": []})
    _pack(tmp_path, "demo-theatre", {"title": "Theatre", "use_case": "/solutions/t/"})
    _pack(tmp_path, "demo-cyber", {"title": " Cyber ", "use_case": "/solutions/"})
    (tmp_path / "demo-notes.json").write_text("{}", encoding="utf-8")

    packs = quickstart_json.demo_packs(image=IMAGE, root=tmp_path, version="1.2.3")

    assert [(pack["name"], pack["title"]) for pack in packs] == [
        ("cyber", "Cyber"),
        ("theatre", "Theatre"),
    ]
    assert packs[1]["pinned"] == [
        f"docker compose -f oci://{IMAGE}:demo-theatre-1.2.3 up -d"
    ]
    assert (
        quickstart_json.demo_packs(image=IMAGE, root=tmp_path / "x", version="1") == []
    )


@pytest.mark.parametrize(
    ("name", "manifest", "match"),
    [
        pytest.param("demo-a", "[", "not readable JSON", id="malformed"),
        pytest.param("demo-a", [], "no JSON object", id="not-an-object"),
        pytest.param("demo-a", {"use_case": "/solutions/"}, "`title`", id="no-title"),
        pytest.param(
            "demo-a", {"title": " ", "use_case": "/solutions/"}, "`title`", id="blank"
        ),
        pytest.param("demo-a", {"title": "A"}, "`use_case`", id="no-use-case"),
        pytest.param(
            "demo-a",
            {"title": "A", "use_case": "https://hyperi.io/solutions/"},
            "`use_case`",
            id="full-url",
        ),
        pytest.param(
            "demo-a", {"title": "A", "use_case": "solutions"}, "`use_case`", id="bare"
        ),
        pytest.param(
            "demo-Cyber",
            {"title": "A", "use_case": "/solutions/"},
            "lower-case words",
            id="bad-name",
        ),
    ],
)
def test_a_pack_it_cannot_describe_is_refused(
    manifest: object, match: str, name: str, tmp_path: Path
) -> None:
    _pack(tmp_path, name, manifest)

    with pytest.raises(quickstart_json.QuickstartJsonError, match=match):
        quickstart_json.demo_packs(image=IMAGE, root=tmp_path, version="1.2.3")


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

    default = quickstart_json.default_profile()
    document = quickstart_json.quickstart(
        default=default,
        image="ghcr.io/hyperi-io/dfe-docker",
        profiles=profiles,
        stack_version="2.2.2",
        version="9.9.9",
    )

    assert document["schema"] == 2
    assert document["kind"] == "compose"
    assert document["packs"][0] == {
        "name": "cyber",
        "title": "Cyber",
        "use_case": "/solutions/",
        "latest": [
            "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-cyber up -d"
        ],
        "pinned": [
            "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-cyber-9.9.9 up -d"
        ],
    }
    assert [pack["name"] for pack in document["packs"]] == demo_pack_names()
    assert [entry["name"] for entry in document["profiles"]] == [default]
    assert document["profiles"][0]["default"] is True
    assert document["profiles"][0]["transport"] in {"grpc", "kafka"}
    assert {key: document[key] for key in ("up", "demo")} == {
        "up": {
            "latest": [
                "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose up -d"
            ],
            "pinned": [
                "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose-9.9.9 up -d"
            ],
        },
        "demo": {
            "latest": [
                "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo up -d"
            ],
            "pinned": [
                "docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-9.9.9 up -d"
            ],
        },
    }
    assert quickstart_json._version_key(document["min_compose"]) >= (2, 34, 0)
    assert document["login"] == f"admin / {FIRST_LOGIN_PASSWORD}"
    assert "dfe-docker:compose down`" in document["notes"]["up"]
    assert "dfe-docker:demo down -v`" in document["notes"]["demo"]
    for note in document["notes"].values():
        assert FIRST_LOGIN_PASSWORD in note
        assert ";" not in note


def test_an_unknown_default_profile_is_refused() -> None:
    with pytest.raises(quickstart_json.QuickstartJsonError, match="default profile"):
        quickstart_json.quickstart(
            default="single",
            image="ghcr.io/hyperi-io/dfe-docker",
            profiles={"slim-grpc": {"transport": "grpc"}},
            stack_version="2.2.2",
            version="9.9.9",
        )
