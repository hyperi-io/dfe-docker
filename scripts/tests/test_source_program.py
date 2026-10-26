#  Project:      dfe-docker
#  File:         tests/test_source_program.py
#  Purpose:      Assert test_source.py hands the vector case the checkout its pipeline comes from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Only the vector case names a program checkout, the dfe-transform-vector one its pipeline is uploaded from."""

from pathlib import Path

import pytest

import test_source


def _program_checkout(*, case: str, flag: str | None) -> Path | None:
    """The checkout test_source.py passes as --program-repo for ``case``."""
    return test_source._case_repo(
        case=case,
        cases=test_source.PROGRAM_CASES,
        env_var=test_source.PROGRAM_REPO_ENV_VAR,
        flag=flag,
        marker=test_source.PROGRAM_MARKER,
    )


@pytest.fixture
def vector_checkout(tmp_path: Path) -> Path:
    """A directory shaped like a dfe-transform-vector checkout."""
    marker = tmp_path / test_source.PROGRAM_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("transforms: {}\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("case", ["filebeat", "elastic", "cloudwatch"])
def test_only_the_vector_case_names_a_program_checkout(
    monkeypatch: pytest.MonkeyPatch, vector_checkout: Path, case: str
) -> None:
    monkeypatch.setenv(test_source.PROGRAM_REPO_ENV_VAR, str(vector_checkout))
    assert _program_checkout(case=case, flag=None) is None


def test_the_vector_case_reads_its_checkout_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, vector_checkout: Path
) -> None:
    monkeypatch.setenv(test_source.PROGRAM_REPO_ENV_VAR, str(vector_checkout))
    assert _program_checkout(case="vector", flag=None) == vector_checkout.resolve()


def test_the_flag_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vector_checkout: Path
) -> None:
    monkeypatch.setenv(test_source.PROGRAM_REPO_ENV_VAR, str(tmp_path / "elsewhere"))
    found = _program_checkout(case="vector", flag=str(vector_checkout))
    assert found == vector_checkout.resolve()


def test_an_unnamed_checkout_leaves_the_runner_to_find_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(test_source.PROGRAM_REPO_ENV_VAR, raising=False)
    assert _program_checkout(case="vector", flag=None) is None


def test_a_checkout_with_no_pipeline_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="filebeat.yaml"):
        _program_checkout(case="vector", flag=str(tmp_path))
