#  Project:      dfe-docker
#  File:         scripts/tests/test_proof.py
#  Purpose:      Assert the unattended proof always restores the stack, reports
#                every failure and leaves nothing behind
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""proof.main() step ordering, exit status and clean-up, with every step faked.

A host runs this on a timer forever, so three properties matter more than any one
test: the standing stack is restored even after the e2e suite fails, a failure
anywhere is a non-zero exit, and the work directory is gone afterwards.
"""

from pathlib import Path

import pytest

import proof


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Fake make steps; ``outcome`` sets each step's result, ``ran`` records order."""
    state: dict = {
        "ran": [],
        "outcome": {},
        "work": None,
        "applied": tmp_path / ".dfe-stack-applied",
        "proven": tmp_path / ".dfe-proof-passed",
    }
    work = tmp_path / "work"

    def fake_mkdtemp(prefix: str) -> str:
        work.mkdir()
        (work / "e2e-shots").mkdir()
        (work / "e2e-shots" / "shot.png").write_bytes(b"png")
        state["work"] = work
        return str(work)

    def fake_run_step(name: str, argv: list[str], env: dict[str, str]) -> bool:
        state["ran"].append(name)
        assert env["DFE_E2E_SHOTS_DIR"] == str(work / "e2e-shots")
        return state["outcome"].get(name, True)

    def fake_source_step(work_dir: Path, env: dict[str, str]) -> bool:
        state["ran"].append("source")
        return state["outcome"].get("source", True)

    monkeypatch.setattr(proof.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(proof, "run_step", fake_run_step)
    monkeypatch.setattr(proof, "source_step", fake_source_step)
    monkeypatch.setattr(proof, "APPLIED_MARKER", state["applied"])
    monkeypatch.setattr(proof, "PROVEN_MARKER", state["proven"])
    monkeypatch.delenv("DFE_PROOF_SKIP", raising=False)
    monkeypatch.delenv("DFE_PROOF_FORCE", raising=False)
    return state


def test_a_pass_marks_the_applied_version_proven(steps: dict) -> None:
    steps["applied"].write_text("2.2.1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.1\n"


def test_a_proven_version_is_not_run_again(steps: dict) -> None:
    steps["applied"].write_text("2.2.1\n", encoding="utf-8")
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["ran"] == []
    assert steps["work"] is None


def test_a_new_version_runs_although_an_older_one_is_proven(steps: dict) -> None:
    steps["applied"].write_text("2.2.2\n", encoding="utf-8")
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["ran"] == ["e2e", "restore", "source"]
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.2\n"


def test_a_failure_leaves_the_version_unproven(steps: dict) -> None:
    steps["applied"].write_text("2.2.2\n", encoding="utf-8")
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    steps["outcome"]["source"] = False
    assert proof.main() == 1
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.1\n"


def test_force_runs_a_proven_version(
    steps: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps["applied"].write_text("2.2.1\n", encoding="utf-8")
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    monkeypatch.setenv("DFE_PROOF_FORCE", "1")
    assert proof.main() == 0
    assert steps["ran"] == ["e2e", "restore", "source"]


def test_a_partial_run_never_marks_proven(
    steps: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps["applied"].write_text("2.2.1\n", encoding="utf-8")
    monkeypatch.setenv("DFE_PROOF_SKIP", "e2e")
    assert proof.main() == 0
    assert not steps["proven"].exists()


def test_no_applied_marker_always_runs_and_marks_nothing(steps: dict) -> None:
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["ran"] == ["e2e", "restore", "source"]
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.1\n"


def test_all_steps_pass(steps: dict) -> None:
    assert proof.main() == 0
    assert steps["ran"] == ["e2e", "restore", "source"]
    assert not steps["work"].exists()


def test_failed_e2e_still_restores_and_fails(steps: dict) -> None:
    steps["outcome"]["e2e"] = False
    assert proof.main() == 1
    assert steps["ran"] == ["e2e", "restore", "source"]
    assert not steps["work"].exists()


def test_failed_restore_skips_source_and_fails(steps: dict) -> None:
    steps["outcome"]["restore"] = False
    assert proof.main() == 1
    assert steps["ran"] == ["e2e", "restore"]
    assert not steps["work"].exists()


def test_failed_source_fails(steps: dict) -> None:
    steps["outcome"]["source"] = False
    assert proof.main() == 1
    assert not steps["work"].exists()


def test_skip_e2e_runs_source_only(
    steps: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DFE_PROOF_SKIP", "e2e")
    assert proof.main() == 0
    assert steps["ran"] == ["source"]


def test_nothing_ran_is_a_failure(steps: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFE_PROOF_SKIP", "e2e,source")
    assert proof.main() == 1
    assert steps["ran"] == []


def test_work_dir_removed_when_a_step_raises(
    steps: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(name: str, argv: list[str], env: dict[str, str]) -> bool:
        raise OSError("make missing")

    monkeypatch.setattr(proof, "run_step", boom)
    with pytest.raises(OSError):
        proof.main()
    assert not steps["work"].exists()


def test_infra_ref_prefers_the_applied_stack_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / ".dfe-stack-applied"
    marker.write_text("2.2.1-rc.6\n", encoding="utf-8")
    monkeypatch.setattr(proof, "APPLIED_MARKER", marker)
    monkeypatch.delenv("DFE_PROOF_INFRA_REF", raising=False)
    monkeypatch.setattr(proof, "remote_has_ref", lambda repo, ref: True)
    assert proof.infra_ref("repo") == "2.2.1-rc.6"
    monkeypatch.setattr(proof, "remote_has_ref", lambda repo, ref: False)
    assert proof.infra_ref("repo") == ""
    monkeypatch.setenv("DFE_PROOF_INFRA_REF", "main")
    assert proof.infra_ref("repo") == "main"


def test_source_step_refuses_without_chrome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(proof.shutil, "which", lambda name: None)
    assert proof.source_step(tmp_path, {}) is False
