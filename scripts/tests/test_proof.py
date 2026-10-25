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
anywhere is a non-zero exit, and the work directory is gone afterwards. The run
also excludes the daemon updater through the stack lock, refuses a user who does
not own .env, and tests the dfe-engine version the stack pins.
"""

from pathlib import Path

import pytest

import _stack_lock
import proof


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Fake make steps; ``outcome`` sets each step's result, ``ran`` records order."""
    state: dict = {
        "ran": [],
        "outcome": {},
        "work": None,
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
    monkeypatch.setattr(proof, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(proof, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(proof, "PROVEN_MARKER", state["proven"])
    monkeypatch.delenv("DFE_PROOF_SKIP", raising=False)
    monkeypatch.delenv("DFE_PROOF_FORCE", raising=False)
    monkeypatch.delenv("DFE_STACK_VERSION", raising=False)

    def pin(version: str) -> None:
        (tmp_path / ".env").write_text(
            f"DFE_STACK_VERSION={version}\n", encoding="utf-8"
        )

    state["pin"] = pin
    return state


def test_a_pass_marks_the_pinned_version_proven(steps: dict) -> None:
    steps["pin"]("2.2.1")
    assert proof.main() == 0
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.1\n"


def test_a_proven_version_is_not_run_again(steps: dict) -> None:
    steps["pin"]("2.2.1")
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["ran"] == []
    assert steps["work"] is None


def test_a_new_version_runs_although_an_older_one_is_proven(steps: dict) -> None:
    steps["pin"]("2.2.2")
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["ran"] == ["e2e", "restore", "source"]
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.2\n"


def test_a_failure_leaves_the_version_unproven(steps: dict) -> None:
    steps["pin"]("2.2.2")
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    steps["outcome"]["source"] = False
    assert proof.main() == 1
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.1\n"


def test_force_runs_a_proven_version(
    steps: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps["pin"]("2.2.1")
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    monkeypatch.setenv("DFE_PROOF_FORCE", "1")
    assert proof.main() == 0
    assert steps["ran"] == ["e2e", "restore", "source"]


def test_a_partial_run_never_marks_proven(
    steps: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps["pin"]("2.2.1")
    monkeypatch.setenv("DFE_PROOF_SKIP", "e2e")
    assert proof.main() == 0
    assert not steps["proven"].exists()


def test_no_pinned_version_always_runs_and_marks_nothing(steps: dict) -> None:
    steps["proven"].write_text("2.2.1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["ran"] == ["e2e", "restore", "source"]
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.1\n"


def test_a_pass_marks_the_version_env_pins_not_the_daemons_last_apply(
    steps: dict, tmp_path: Path
) -> None:
    steps["pin"]("2.2.1-rc.8")
    (tmp_path / ".dfe-stack-applied").write_text("2.2.1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["proven"].read_text(encoding="utf-8") == "2.2.1-rc.8\n"


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


def test_infra_ref_prefers_the_pinned_stack_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DFE_STACK_VERSION=2.2.1-rc.6\n", encoding="utf-8")
    monkeypatch.setattr(proof, "ENV_FILE", env_file)
    monkeypatch.delenv("DFE_STACK_VERSION", raising=False)
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


def test_a_user_who_does_not_own_env_is_refused(
    steps: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("CLICKHOUSE_PASSWORD=hunter2\n", encoding="utf-8")
    owner = (tmp_path / ".env").stat().st_uid
    monkeypatch.setattr(proof.os, "getuid", lambda: owner + 1)
    assert proof.main() == 1
    shown = capsys.readouterr().out
    assert f"uid {owner}" in shown
    assert f"sudo -u '#{owner}' make proof" in shown
    assert "hunter2" not in shown
    assert steps["ran"] == []
    assert steps["work"] is None


def test_the_owner_of_env_runs_the_proof(steps: dict, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    assert proof.main() == 0
    assert steps["ran"] == ["e2e", "restore", "source"]


def test_a_proof_does_not_start_while_the_stack_lock_is_held(
    steps: dict, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _stack_lock.stack_lock(directory=tmp_path) as held:
        assert held
        assert proof.main() == 1
    assert steps["ran"] == []
    assert "nothing was run" in capsys.readouterr().out


def test_every_step_runs_under_the_stack_lock_and_it_is_released_after(
    steps: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    held_during: list[bool] = []

    def probing_run_step(name: str, argv: list[str], env: dict[str, str]) -> bool:
        with _stack_lock.stack_lock(directory=tmp_path) as other:
            held_during.append(not other)
        return True

    monkeypatch.setattr(proof, "run_step", probing_run_step)
    monkeypatch.setattr(proof, "source_step", lambda work, env: True)
    assert proof.main() == 0
    assert held_during == [True, True]
    with _stack_lock.stack_lock(directory=tmp_path) as free:
        assert free


@pytest.fixture
def engine_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An .env for proof.engine_ref to read, with the environment cleared."""
    env_file = tmp_path / ".env"
    monkeypatch.setattr(proof, "ENV_FILE", env_file)
    monkeypatch.delenv("DFE_PROOF_ENGINE_REF", raising=False)
    monkeypatch.delenv("DFE_ENGINE_VERSION", raising=False)
    monkeypatch.delenv("DFE_PROOF_TRANSFORM_VRL_REF", raising=False)
    monkeypatch.delenv("DFE_TRANSFORM_VRL_VERSION", raising=False)
    monkeypatch.delenv("DFE_PROOF_TRANSFORM_VECTOR_REF", raising=False)
    monkeypatch.delenv("DFE_TRANSFORM_VECTOR_VERSION", raising=False)
    monkeypatch.setattr(proof, "remote_has_ref", lambda repo, ref: True)
    return env_file


def test_engine_ref_is_the_pinned_tag_without_its_digest(engine_pin: Path) -> None:
    engine_pin.write_text(
        f"DFE_ENGINE_VERSION=v1.22.16@sha256:{'a' * 64}\n", encoding="utf-8"
    )
    assert proof.engine_ref("repo") == "v1.22.16"


def test_engine_ref_reads_the_environment_before_env(
    engine_pin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_pin.write_text("DFE_ENGINE_VERSION=v1.0.0\n", encoding="utf-8")
    monkeypatch.setenv("DFE_ENGINE_VERSION", "v1.22.16")
    assert proof.engine_ref("repo") == "v1.22.16"


def test_engine_ref_override_wins(
    engine_pin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_pin.write_text("DFE_ENGINE_VERSION=v1.22.16\n", encoding="utf-8")
    monkeypatch.setenv("DFE_PROOF_ENGINE_REF", "my-branch")
    assert proof.engine_ref("repo") == "my-branch"


def test_engine_ref_falls_back_when_the_tag_is_not_published(
    engine_pin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_pin.write_text("DFE_ENGINE_VERSION=v9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(proof, "remote_has_ref", lambda repo, ref: False)
    assert proof.engine_ref("repo") == ""


def test_engine_ref_is_the_default_branch_with_no_pin(engine_pin: Path) -> None:
    assert proof.engine_ref("repo") == ""


def test_transform_vrl_ref_is_its_pinned_tag(engine_pin: Path) -> None:
    engine_pin.write_text(
        f"DFE_TRANSFORM_VRL_VERSION=v1.1.33@sha256:{'b' * 64}\n", encoding="utf-8"
    )
    assert proof.transform_vrl_ref("repo") == "v1.1.33"


def test_transform_vrl_ref_override_wins(
    engine_pin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_pin.write_text("DFE_TRANSFORM_VRL_VERSION=v1.1.33\n", encoding="utf-8")
    monkeypatch.setenv("DFE_PROOF_TRANSFORM_VRL_REF", "my-branch")
    assert proof.transform_vrl_ref("repo") == "my-branch"


def test_transform_vector_ref_is_its_pinned_tag(engine_pin: Path) -> None:
    engine_pin.write_text(
        f"DFE_TRANSFORM_VECTOR_VERSION=v1.0.41@sha256:{'c' * 64}\n", encoding="utf-8"
    )
    assert proof.transform_vector_ref("repo") == "v1.0.41"


def test_transform_vector_ref_override_wins(
    engine_pin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_pin.write_text("DFE_TRANSFORM_VECTOR_VERSION=v1.0.41\n", encoding="utf-8")
    monkeypatch.setenv("DFE_PROOF_TRANSFORM_VECTOR_REF", "my-branch")
    assert proof.transform_vector_ref("repo") == "my-branch"


def test_each_case_keeps_its_own_screenshots(tmp_path: Path) -> None:
    assert proof.source_args("vector", tmp_path) == (
        f"--case vector --shots-dir {tmp_path / 'source-shots' / 'vector'}"
    )


@pytest.fixture
def source_walk(engine_pin: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """source_step with the clones and make steps faked, recording what each would do."""
    engine_pin.write_text(
        f"DFE_ENGINE_VERSION=v1.22.16@sha256:{'a' * 64}\n"
        f"DFE_TRANSFORM_VRL_VERSION=v1.1.33@sha256:{'b' * 64}\n"
        f"DFE_TRANSFORM_VECTOR_VERSION=v1.0.41@sha256:{'c' * 64}\n",
        encoding="utf-8",
    )
    state: dict = {"cloned": {}, "steps": [], "env": {}, "outcome": {}}
    monkeypatch.setattr(proof.shutil, "which", lambda name: "/usr/bin/chrome")
    monkeypatch.setattr(
        proof,
        "clone",
        lambda repo, ref, dest: state["cloned"].update({dest.name: ref}),
    )
    monkeypatch.setattr(proof, "infra_ref", lambda repo: "2.2.1")
    monkeypatch.setattr(proof, "acceptance_python", lambda infra, venv: "python")

    def fake_run_step(name: str, argv: list[str], env: dict[str, str]) -> bool:
        state["steps"].append(name)
        state["env"][name] = env
        return state["outcome"].get(name, True)

    monkeypatch.setattr(proof, "run_step", fake_run_step)
    return state


def test_the_source_walk_clones_its_repos_at_the_pinned_tags(
    source_walk: dict, tmp_path: Path
) -> None:
    assert proof.source_step(tmp_path, {}) is True
    assert source_walk["cloned"] == {
        "dfe-infra": "2.2.1",
        "dfe-engine": "v1.22.16",
        "dfe-transform-vrl": "v1.1.33",
        "dfe-transform-vector": "v1.0.41",
    }
    env = source_walk["env"]["source vector"]
    assert env["DFE_TRANSFORM_VRL_REPO"] == str(tmp_path / "dfe-transform-vrl")
    assert env["DFE_TRANSFORM_VECTOR_REPO"] == str(tmp_path / "dfe-transform-vector")


def test_the_source_walk_runs_the_vector_case_after_filebeat(
    source_walk: dict, tmp_path: Path
) -> None:
    assert proof.source_step(tmp_path, {}) is True
    assert source_walk["steps"] == ["source filebeat", "source vector"]
    assert source_walk["env"]["source vector"]["SOURCE_ARGS"].startswith(
        "--case vector "
    )


def test_a_failed_case_fails_the_walk_and_the_next_still_runs(
    source_walk: dict, tmp_path: Path
) -> None:
    source_walk["outcome"]["source filebeat"] = False
    assert proof.source_step(tmp_path, {}) is False
    assert source_walk["steps"] == ["source filebeat", "source vector"]
