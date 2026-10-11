#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/proof.py
#  Purpose:      Unattended acceptance run for a host that updates itself
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Unattended acceptance run for a host that keeps a stack running and updates it.

Three steps, in order:

1. ``make test-e2e`` -- every non-outage e2e test, search-rule-hunt included.
   It brings up one stack per test, so the standing stack is gone afterwards.
2. ``make ci`` -- the standing stack back up, and POST with it. Runs even when
   step 1 failed, so the host is never left without a stack.
3. ``make test-source`` -- the console source walk against that stack, from
   fresh dfe-infra and dfe-engine checkouts and a throwaway Playwright
   environment built from dfe-infra's ``scripts/acceptance/requirements.txt``.

Everything it creates lives in one temporary directory that is deleted at the
end, pass or fail: the checkouts, the virtualenv and every screenshot. Nothing
accumulates between runs, so a host can run this on a timer indefinitely.

The browser is the host's installed Chrome (the runner drives ``--channel
chrome``); this script never downloads one.

Environment:
    DFE_PROOF_INFRA_REPO   dfe-infra clone URL (default: the public repo)
    DFE_PROOF_INFRA_REF    branch or tag; default is the version in
                           .dfe-stack-applied when that tag exists, else the
                           default branch
    DFE_PROOF_ENGINE_REPO  dfe-engine clone URL (default: the public repo)
    DFE_PROOF_ENGINE_REF   branch or tag (default: the default branch)
    DFE_PROOF_SKIP         comma-separated steps to skip: e2e, source

Exit 0 only when every step that ran passed.

Usage:
    make proof
    DFE_PROOF_SKIP=e2e make proof        # source walk only, against the running stack
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
APPLIED_MARKER = PROJECT_DIR / ".dfe-stack-applied"
DEFAULT_INFRA_REPO = "https://github.com/hyperi-io/dfe-infra.git"
DEFAULT_ENGINE_REPO = "https://github.com/hyperi-io/dfe-engine.git"
ACCEPTANCE_REQUIREMENTS = Path("scripts") / "acceptance" / "requirements.txt"
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chrome")


def log(message: str) -> None:
    print(f"proof: {message}", flush=True)


def run_step(name: str, argv: list[str], env: dict[str, str]) -> bool:
    """Run one make step from the checkout and report its outcome and duration."""
    log(f"START {name}: {' '.join(argv)}")
    started = time.monotonic()
    result = subprocess.run(argv, cwd=PROJECT_DIR, env=env, check=False)
    minutes = (time.monotonic() - started) / 60
    outcome = "PASS" if result.returncode == 0 else f"FAIL (exit {result.returncode})"
    log(f"{outcome} {name} in {minutes:.1f} min")
    return result.returncode == 0


def remote_has_ref(repo: str, ref: str) -> bool:
    probe = subprocess.run(
        ["git", "ls-remote", "--exit-code", repo, ref],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def infra_ref(repo: str) -> str:
    """The dfe-infra ref matching the running stack, so the runner is the one it shipped with."""
    explicit = os.environ.get("DFE_PROOF_INFRA_REF", "").strip()
    if explicit:
        return explicit
    if APPLIED_MARKER.is_file():
        applied = APPLIED_MARKER.read_text(encoding="utf-8").strip()
        if applied and remote_has_ref(repo, f"refs/tags/{applied}"):
            return applied
    return ""


def clone(repo: str, ref: str, dest: Path) -> None:
    argv = ["git", "clone", "--quiet", "--depth", "1"]
    if ref:
        argv += ["--branch", ref]
    subprocess.run([*argv, repo, str(dest)], check=True)
    log(f"cloned {repo} at {ref or 'the default branch'}")


def acceptance_python(infra: Path, venv: Path) -> str:
    """A throwaway interpreter carrying dfe-infra's acceptance requirements."""
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is not on PATH")
    subprocess.run([uv, "venv", "--quiet", str(venv)], check=True)
    python = venv / "bin" / "python"
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--quiet",
            "--python",
            str(python),
            "-r",
            str(infra / ACCEPTANCE_REQUIREMENTS),
        ],
        check=True,
    )
    return str(python)


def source_step(work: Path, env: dict[str, str]) -> bool:
    if not any(shutil.which(name) for name in CHROME_NAMES):
        log("FAIL source: no Chrome on PATH; the runner drives the installed browser")
        return False
    infra_repo = os.environ.get("DFE_PROOF_INFRA_REPO", DEFAULT_INFRA_REPO)
    engine_repo = os.environ.get("DFE_PROOF_ENGINE_REPO", DEFAULT_ENGINE_REPO)
    try:
        clone(infra_repo, infra_ref(infra_repo), work / "dfe-infra")
        clone(
            engine_repo,
            os.environ.get("DFE_PROOF_ENGINE_REF", "").strip(),
            work / "dfe-engine",
        )
        python = acceptance_python(work / "dfe-infra", work / "venv")
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        log(f"FAIL source setup: {exc}")
        return False
    step_env = {
        **env,
        "DFE_INFRA_DIR": str(work / "dfe-infra"),
        "DFE_ENGINE_REPO": str(work / "dfe-engine"),
        "DFE_ACCEPTANCE_PYTHON": python,
        "SOURCE_ARGS": f"--shots-dir {work / 'source-shots'}",
    }
    return run_step("source", ["make", "--no-print-directory", "test-source"], step_env)


def main() -> int:
    skip = {
        s.strip() for s in os.environ.get("DFE_PROOF_SKIP", "").split(",") if s.strip()
    }
    results: dict[str, bool] = {}
    work = Path(tempfile.mkdtemp(prefix="dfe-proof-"))
    try:
        env = {**os.environ, "DFE_E2E_SHOTS_DIR": str(work / "e2e-shots")}
        if "e2e" not in skip:
            results["e2e"] = run_step(
                "e2e", ["make", "--no-print-directory", "test-e2e"], env
            )
            results["restore"] = run_step(
                "restore", ["make", "--no-print-directory", "ci"], env
            )
        if "source" not in skip:
            if results.get("restore", True):
                results["source"] = source_step(work, env)
            else:
                log("SKIP source: the standing stack did not come back")
                results["source"] = False
    finally:
        shutil.rmtree(work, ignore_errors=True)
    summary = " ".join(
        f"{name}={'pass' if ok else 'FAIL'}" for name, ok in results.items()
    )
    log(f"RESULT {summary or 'nothing ran'}")
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
