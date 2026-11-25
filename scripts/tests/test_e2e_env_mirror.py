#  Project:      dfe-docker
#  File:         tests/test_e2e_env_mirror.py
#  Purpose:      Assert the e2e suite reads the engine's env files from a copy taken out of its container
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The engine writes custom keys and instance indexes into a bind on the daemon's side, so the client copies them out through the daemon API before it reads them.

Against a remote DOCKER_HOST the client's own `.tmp/e2e/<run>/env` stays empty, and reading it silently saw no instance. `docker cp` is stood in for at the suite's own `run_cmd` boundary.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import _sources
import test_e2e


@pytest.fixture
def mirror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(name="TMP_DIR", target=test_e2e, value=tmp_path)
    monkeypatch.setattr(name="SUITE_ENV_MIRROR", target=test_e2e, value=tmp_path / "m")
    monkeypatch.setattr(test_e2e, "container_id", lambda service: "engine-1")
    return tmp_path / "m"


def _engine_holds(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, str], returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def _run_cmd(args, capture=False, cwd=None):
        calls.append(list(args))
        if returncode == 0:
            for name, text in files.items():
                (Path(args[-1]) / name).write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout="",
            stderr="" if returncode == 0 else "Error: no such container",
        )

    monkeypatch.setattr(test_e2e, "run_cmd", _run_cmd)
    return calls


def test_the_engine_env_directory_is_copied_out_of_its_container(
    monkeypatch: pytest.MonkeyPatch, mirror: Path
) -> None:
    calls = _engine_holds(monkeypatch, {"dfe-loader.custom.env": "A=1\n"})

    test_e2e.pull_suite_env()

    assert calls == [["docker", "cp", "engine-1:/app/app-env/.", str(mirror)]]
    assert (mirror / "dfe-loader.custom.env").read_text(encoding="utf-8") == "A=1\n"


def test_a_file_the_engine_no_longer_holds_is_gone_from_the_copy(
    monkeypatch: pytest.MonkeyPatch, mirror: Path
) -> None:
    mirror.mkdir()
    (mirror / "stale.custom.env").write_text("OLD=1\n", encoding="utf-8")
    _engine_holds(monkeypatch, {"fresh.custom.env": "NEW=1\n"})

    test_e2e.pull_suite_env()

    assert sorted(path.name for path in mirror.iterdir()) == ["fresh.custom.env"]


def test_a_failed_copy_is_an_error_naming_what_docker_said(
    monkeypatch: pytest.MonkeyPatch, mirror: Path
) -> None:
    _engine_holds(monkeypatch, {}, returncode=1)

    with pytest.raises(test_e2e.SuiteEnvError, match="no such container"):
        test_e2e.pull_suite_env()


def test_no_engine_container_is_an_error_not_an_empty_copy(
    monkeypatch: pytest.MonkeyPatch, mirror: Path
) -> None:
    monkeypatch.setattr(test_e2e, "container_id", lambda service: "")
    calls = _engine_holds(monkeypatch, {})

    with pytest.raises(test_e2e.SuiteEnvError, match="no container"):
        test_e2e.pull_suite_env()

    assert calls == []


def test_the_instance_index_is_read_from_the_engine_copy_never_the_local_bind(
    monkeypatch: pytest.MonkeyPatch, mirror: Path
) -> None:
    source = _sources.Source(engine="vrl", files=(), name="edge-syslog")
    test_e2e.SUITE_ENV_DIR.mkdir(parents=True, exist_ok=True)
    (test_e2e.SUITE_ENV_DIR / f"{source.app}.instances").write_text(
        "other\n", encoding="utf-8"
    )
    _engine_holds(monkeypatch, {f"{source.app}.instances": "edge-syslog\n"})

    found = test_e2e.read_index(sources=[source])

    assert found == {source.app: ["edge-syslog"]}


def test_a_source_the_engine_never_indexed_is_not_found(
    monkeypatch: pytest.MonkeyPatch, mirror: Path
) -> None:
    source = _sources.Source(engine="vrl", files=(), name="edge-syslog")
    _engine_holds(monkeypatch, {})

    assert test_e2e.read_index(sources=[source]) == {}


def test_awaiting_the_index_fails_the_test_when_the_copy_fails(
    monkeypatch: pytest.MonkeyPatch, mirror: Path
) -> None:
    _engine_holds(monkeypatch, {}, returncode=1)
    source = _sources.Source(engine="vrl", files=(), name="edge-syslog")
    ctx = test_e2e.TestContext()

    found = test_e2e.await_index(
        ctx=ctx, test=SimpleNamespace(name="t", sources=[source])
    )

    assert found is None
    assert ctx.failed == 1
    assert "no such container" in ctx.failures[0]
