#  Project:      dfe-docker
#  File:         tests/test_demo_archive.py
#  Purpose:      Assert the demo's archive tool keeps the archive small, clears a source and names what it holds, and that the pack manifest matches the source files
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on the demo pack that need no stack: resources/demo/archive.py over a temporary archive, and demo.json against resources/demo/sources."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

import demo
from _common import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "demo_archive", REPO_ROOT / "resources" / "demo" / "archive.py"
)
archive = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(archive)

NOW = 1_800_000_000.0


def _object(root: Path, name: str, *, age_minutes: float, size: int = 10) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = NOW - age_minutes * 60
    os.utime(path, (stamp, stamp))
    return path


def test_prune_removes_what_is_older_than_the_retention_and_empty_directories(
    tmp_path: Path,
) -> None:
    old = _object(tmp_path, "a_land/default/2026/old.jsonl", age_minutes=20)
    new = _object(tmp_path, "a_land/default/2026/new.jsonl", age_minutes=5)
    _object(tmp_path, "b_land/default/old.jsonl", age_minutes=30)

    removed = archive.prune(
        max_age_seconds=15 * 60, max_bytes=10**9, now=NOW, root=tmp_path
    )

    assert removed == 2
    assert not old.exists()
    assert new.exists()
    assert not (tmp_path / "b_land").exists()


def test_prune_drops_the_oldest_files_until_the_archive_fits_its_cap(
    tmp_path: Path,
) -> None:
    oldest = _object(tmp_path, "a_land/1.jsonl", age_minutes=10, size=60)
    middle = _object(tmp_path, "a_land/2.jsonl", age_minutes=5, size=60)
    newest = _object(tmp_path, "a_land/3.jsonl", age_minutes=1, size=60)

    removed = archive.prune(
        max_age_seconds=15 * 60, max_bytes=100, now=NOW, root=tmp_path
    )

    assert removed == 2
    assert [oldest.exists(), middle.exists(), newest.exists()] == [False, False, True]


def test_purge_empties_only_the_named_sources(tmp_path: Path) -> None:
    _object(tmp_path, "demo-syslog_land/default/1.jsonl", age_minutes=1)
    _object(tmp_path, "demo-syslog_land/default/2.jsonl", age_minutes=1)
    kept = _object(tmp_path, "main_land/default/1.jsonl", age_minutes=1)

    removed = archive.purge(root=tmp_path, sources=["demo-syslog", "absent"])

    assert removed == 2
    assert not (tmp_path / "demo-syslog_land").exists()
    assert kept.exists()


def test_show_names_the_newest_objects_and_a_sample_line(tmp_path: Path) -> None:
    for index in range(5):
        path = _object(
            tmp_path, f"s_land/default/{index}.jsonl", age_minutes=10 - index
        )
        path.write_text(f'{{"n": {index}}}\n', encoding="utf-8")
        os.utime(path, (NOW - (10 - index) * 60, NOW - (10 - index) * 60))

    lines = archive.show(limit=3, root=tmp_path, sources=["s"])

    assert [line.split("  ")[0] for line in lines[:3]] == [
        "s_land/default/2.jsonl",
        "s_land/default/3.jsonl",
        "s_land/default/4.jsonl",
    ]
    assert lines[3] == 'first line of the newest: {"n": 4}'
    assert archive.show(limit=3, root=tmp_path, sources=["none"]) == []


class TestManifest:
    def test_the_committed_manifest_matches_the_committed_sources(self) -> None:
        manifest = demo._read_manifest(path=demo.MANIFEST_FILE)
        loaded = demo.load_demo(events=demo.EVENTS_DIR, sources=demo.SOURCES_DIR)

        demo.check_manifest(demo=loaded, manifest=manifest)

        assert manifest["archive"]["retention_minutes"] <= 15
        assert manifest["archive"]["max_megabytes"] <= 200

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            pytest.param(
                lambda m: m["sources"].append("extra"),
                "Make them the same set",
                id="extra",
            ),
            pytest.param(
                lambda m: m["sources"].remove("fortinet"),
                "Make them the same set",
                id="missing",
            ),
            pytest.param(
                lambda m: m["archive"]["sources"].append("fortinet"),
                "lists archive sources",
                id="archive_mismatch",
            ),
        ],
    )
    def test_refuses_a_manifest_that_disagrees_with_the_sources(
        self, change, message: str
    ) -> None:
        manifest = json.loads(demo.MANIFEST_FILE.read_text(encoding="utf-8"))
        change(manifest)
        loaded = demo.load_demo(events=demo.EVENTS_DIR, sources=demo.SOURCES_DIR)

        with pytest.raises(demo.DemoError, match=message):
            demo.check_manifest(demo=loaded, manifest=manifest)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("[", id="malformed"),
            pytest.param('{"sources": []}', id="no_archive"),
            pytest.param(
                '{"sources": ["a"], "archive": {"sources": [], "max_megabytes": 0, "retention_minutes": 1, "sweep_minutes": 1}}',
                id="zero_cap",
            ),
        ],
    )
    def test_refuses_an_unreadable_manifest(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / "demo.json"
        path.write_text(text, encoding="utf-8")

        with pytest.raises(demo.DemoError):
            demo._read_manifest(path=path)
