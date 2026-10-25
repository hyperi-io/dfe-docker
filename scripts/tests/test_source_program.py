#  Project:      dfe-docker
#  File:         tests/test_source_program.py
#  Purpose:      Assert test_source.py hands the vector case the checkout its pipeline comes from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The program checkout the vector case uploads from, and the cases that take none.

The vector case grades against dfe-transform-vrl's corpus and uploads
dfe-transform-vector's pipeline, so it names a second checkout. Every other case
passes none, and the runner then looks beside the engine repo.
"""

from pathlib import Path

import pytest

import test_source


@pytest.fixture
def vector_checkout(tmp_path: Path) -> Path:
    """A directory shaped like a dfe-transform-vector checkout."""
    marker = tmp_path / test_source.PROGRAM_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("transforms: {}\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("case", ["filebeat", "elastic", "cloudwatch"])
def test_only_the_vector_case_names_a_program_checkout(
    case: str, vector_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(test_source.PROGRAM_REPO_ENV_VAR, str(vector_checkout))
    assert test_source._program_repo(None, case) is None


def test_the_vector_case_reads_its_checkout_from_the_environment(
    vector_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(test_source.PROGRAM_REPO_ENV_VAR, str(vector_checkout))
    assert test_source._program_repo(None, "vector") == vector_checkout.resolve()


def test_the_flag_beats_the_environment(
    vector_checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(test_source.PROGRAM_REPO_ENV_VAR, str(tmp_path / "elsewhere"))
    found = test_source._program_repo(str(vector_checkout), "vector")
    assert found == vector_checkout.resolve()


def test_an_unnamed_checkout_leaves_the_runner_to_find_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(test_source.PROGRAM_REPO_ENV_VAR, raising=False)
    assert test_source._program_repo(None, "vector") is None


def test_a_checkout_with_no_pipeline_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="filebeat.yaml"):
        test_source._program_repo(str(tmp_path), "vector")
