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
3. ``make test-source`` - the console source walk against that stack, once per
   case in ``SOURCE_CASES``, from fresh dfe-infra, dfe-engine and transform
   checkouts and a throwaway Playwright environment built from dfe-infra's
   ``scripts/acceptance/requirements.txt``. It needs a profile that runs
   dfe-transform-vrl and dfe-transform-vector per source (single-*) and
   fails at once on one that does not.

Everything it creates lives in one temporary directory that is deleted at the
end, pass or fail: the checkouts, the virtualenv and every screenshot. Nothing
accumulates between runs, so a host can run this on a timer indefinitely.

Each stack version is proven once. A full pass writes the version .env pins
(DFE_STACK_VERSION, what `make stack` last wrote) to .dfe-proof-passed, and a
later run on the same version exits 0 without touching the stack, so a timer
that fires with nothing new does not take the standing stack away. A failed
version runs again on the next fire. A checkout with no pinned version always
runs.

One run at a time. The proof holds the checkout's stack lock (.dfe-stack.lock,
shared with the daemon updater) for its whole run, so an update tick that fires
mid-proof skips itself rather than running ``make ci`` over the e2e stack. A
proof that finds the lock held exits 1 and runs nothing.

The user running it must own .env. Every stack the proof brings up is created
with that user's group (DFE_DEV_GID), so a different user would hand the engine a
group that cannot write the checkout's env/ directory.

The browser is the host's installed Chrome (the runner drives ``--channel
chrome``); this script never downloads one.

Environment:
    DFE_PROOF_INFRA_REPO   dfe-infra clone URL (default: the public repo)
    DFE_PROOF_INFRA_REF    branch or tag; default is the pinned
                           DFE_STACK_VERSION when that tag exists, else the
                           default branch
    DFE_PROOF_ENGINE_REPO  dfe-engine clone URL (default: the public repo)
    DFE_PROOF_ENGINE_REF   branch or tag; default is the tag in DFE_ENGINE_VERSION
                           (the environment, else .env) when that tag exists, else
                           the default branch
    DFE_PROOF_TRANSFORM_VRL_REPO  dfe-transform-vrl clone URL (default: the
                           public repo); the source walk reads its pipeline and
                           corpus
    DFE_PROOF_TRANSFORM_VRL_REF   branch or tag; default is the tag in
                           DFE_TRANSFORM_VRL_VERSION, resolved as for dfe-engine
    DFE_PROOF_TRANSFORM_VECTOR_REPO  dfe-transform-vector clone URL (default:
                           the public repo), whose pipeline the vector case uploads
    DFE_PROOF_TRANSFORM_VECTOR_REF   branch or tag, by default the tag in
                           DFE_TRANSFORM_VECTOR_VERSION, resolved as for dfe-engine
    DFE_PROOF_SKIP         comma-separated steps to skip: e2e, source (a
                           partial run never marks a version proven)
    DFE_PROOF_FORCE        1 to run a version that is already proven

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

# Bytecode for the repo modules imported below goes to .tmp/pycache, as it does under make
sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / ".tmp" / "pycache")

import _common  # noqa: E402
import _stack_lock  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"
PROVEN_MARKER = PROJECT_DIR / ".dfe-proof-passed"
TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_INFRA_REPO = "https://github.com/hyperi-io/dfe-infra.git"
DEFAULT_ENGINE_REPO = "https://github.com/hyperi-io/dfe-engine.git"
DEFAULT_TRANSFORM_VRL_REPO = "https://github.com/hyperi-io/dfe-transform-vrl.git"
DEFAULT_TRANSFORM_VECTOR_REPO = "https://github.com/hyperi-io/dfe-transform-vector.git"
# The app each case routes its source through, run per source only on a profile that sets its `*_enabled` key (slim-grpc does not)
SOURCE_CASE_APPS = {"filebeat": "dfe-transform-vrl", "vector": "dfe-transform-vector"}
# Each pushed case proves one transform's data path, so a pin of either app is proven on its stack version
SOURCE_CASES = tuple(SOURCE_CASE_APPS)
ACCEPTANCE_REQUIREMENTS = Path("scripts") / "acceptance" / "requirements.txt"
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chrome")


def _source_args(*, case: str, work: Path) -> str:
    """The test-source flags for one case, its screenshots kept apart from the others."""
    return f"--case {case} --shots-dir {work / 'source-shots' / case}"


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


def pinned_value(var: str) -> str:
    """A pin `make stack` wrote: the environment's value, else .env's."""
    pinned = os.environ.get(var, "").strip()
    if not pinned and ENV_FILE.is_file():
        values = _common._parse_dotenv(
            text=ENV_FILE.read_text(encoding="utf-8", errors="replace")
        )
        pinned = values.get(var, "").strip()
    return pinned


def stack_version() -> str:
    """The stack version the running stack is pinned to.

    Read from DFE_STACK_VERSION, not .dfe-stack-applied: only the update daemon
    writes that file, so after a manual `make stack VERSION=X` it still names the
    daemon's last apply, and a pass would be recorded against the wrong version.
    """
    return pinned_value("DFE_STACK_VERSION")


def infra_ref(repo: str) -> str:
    """The dfe-infra ref matching the running stack, so the runner is the one it shipped with."""
    explicit = os.environ.get("DFE_PROOF_INFRA_REF", "").strip()
    if explicit:
        return explicit
    version = stack_version()
    if version and remote_has_ref(repo, f"refs/tags/{version}"):
        return version
    return ""


def pinned_ref(repo: str, *, name: str, version_var: str, override_var: str) -> str:
    """The ref of `name` matching the running stack: the override, else the pinned tag, else the default branch."""
    explicit = os.environ.get(override_var, "").strip()
    if explicit:
        return explicit
    tag = pinned_value(version_var).partition("@")[0]
    if not tag:
        return ""
    if remote_has_ref(repo, f"refs/tags/{tag}"):
        return tag
    log(f"WARN {name} has no tag {tag}; testing its default branch instead")
    return ""


def engine_ref(repo: str) -> str:
    """The dfe-engine ref matching the running stack, so the walk tests that version."""
    return pinned_ref(
        repo,
        name="dfe-engine",
        version_var="DFE_ENGINE_VERSION",
        override_var="DFE_PROOF_ENGINE_REF",
    )


def transform_vrl_ref(repo: str) -> str:
    """The dfe-transform-vrl ref matching the running stack: the source walk reads its pipeline and corpus."""
    return pinned_ref(
        repo,
        name="dfe-transform-vrl",
        version_var="DFE_TRANSFORM_VRL_VERSION",
        override_var="DFE_PROOF_TRANSFORM_VRL_REF",
    )


def transform_vector_ref(*, repo: str) -> str:
    """The dfe-transform-vector ref matching the running stack: the vector case uploads its pipeline."""
    return pinned_ref(
        name="dfe-transform-vector",
        override_var="DFE_PROOF_TRANSFORM_VECTOR_REF",
        repo=repo,
        version_var="DFE_TRANSFORM_VECTOR_VERSION",
    )


def env_owner_problem() -> str | None:
    """Why this user may not run the proof, or None when it may.

    A missing .env is not a mismatch: there is nothing for another user to own.
    """
    if not ENV_FILE.is_file():
        return None
    owner = ENV_FILE.stat().st_uid
    me = os.getuid()
    if owner == me:
        return None
    return (
        f"refusing to run as uid {me}: .env belongs to uid {owner}. The stack is "
        "recreated with the running user's group, which cannot write env/ here. "
        f"Run it as the owner: sudo -u '#{owner}' make proof"
    )


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


def source_profile_problem(*, env: dict[str, str]) -> str | None:
    """Why the resolved profile cannot run the source cases, or None when it can."""
    resolved = subprocess.run(
        ["make", "--no-print-directory", "-s", ".tmp/profile.mk"],
        check=False,
        cwd=PROJECT_DIR,
        env=env,
    )
    if resolved.returncode != 0:
        return "the profile did not resolve"
    running = _common._profile_mk_value(key=_common.PER_SOURCE_APPS_VAR)
    missing = [app for app in SOURCE_CASE_APPS.values() if app not in running]
    if not (missing):
        return None
    keys = ", ".join(_common.PER_SOURCE_APP_KEYS[app] for app in missing)
    return (
        f"the profile runs no per-source {', '.join(missing)}, so the engine's "
        f"instance of it is never declared and the cases would wait out their reporting timeouts. "
        f"Run a profile that sets {keys}, such as DFE_PROFILE=single-grpc in .env"
    )


def source_step(work: Path, env: dict[str, str]) -> bool:
    if not any(shutil.which(name) for name in CHROME_NAMES):
        log("FAIL source: no Chrome on PATH; the runner drives the installed browser")
        return False
    problem = source_profile_problem(env=env)
    if problem is not None:
        log(f"FAIL source: {problem}")
        return False
    infra_repo = os.environ.get("DFE_PROOF_INFRA_REPO", DEFAULT_INFRA_REPO)
    engine_repo = os.environ.get("DFE_PROOF_ENGINE_REPO", DEFAULT_ENGINE_REPO)
    vrl_repo = os.environ.get(
        "DFE_PROOF_TRANSFORM_VRL_REPO", DEFAULT_TRANSFORM_VRL_REPO
    )
    vector_repo = os.environ.get(
        "DFE_PROOF_TRANSFORM_VECTOR_REPO", DEFAULT_TRANSFORM_VECTOR_REPO
    )
    try:
        clone(infra_repo, infra_ref(infra_repo), work / "dfe-infra")
        clone(engine_repo, engine_ref(engine_repo), work / "dfe-engine")
        clone(vrl_repo, transform_vrl_ref(vrl_repo), work / "dfe-transform-vrl")
        clone(
            dest=work / "dfe-transform-vector",
            ref=transform_vector_ref(repo=vector_repo),
            repo=vector_repo,
        )
        python = acceptance_python(work / "dfe-infra", work / "venv")
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        log(f"FAIL source setup: {exc}")
        return False
    base_env = {
        **env,
        "DFE_INFRA_DIR": str(work / "dfe-infra"),
        "DFE_ENGINE_REPO": str(work / "dfe-engine"),
        "DFE_TRANSFORM_VRL_REPO": str(work / "dfe-transform-vrl"),
        "DFE_TRANSFORM_VECTOR_REPO": str(work / "dfe-transform-vector"),
        "DFE_ACCEPTANCE_PYTHON": python,
    }
    # Every case runs even after one fails, so one red run names every broken data path rather than the first
    passed = [
        run_step(
            argv=["make", "--no-print-directory", "test-source"],
            env={**base_env, "SOURCE_ARGS": _source_args(case=case, work=work)},
            name=f"source {case}",
        )
        for case in SOURCE_CASES
    ]
    return all(passed)


def read_marker(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def main() -> int:
    problem = env_owner_problem()
    if problem is not None:
        log(f"FAIL {problem}")
        return 1
    with _stack_lock.stack_lock(directory=PROJECT_DIR) as held:
        if not held:
            log(
                "FAIL another proof or an update tick holds "
                f"{_stack_lock.LOCK_FILENAME}; nothing was run"
            )
            return 1
        return run_proof()


def run_proof() -> int:
    skip = {
        s.strip() for s in os.environ.get("DFE_PROOF_SKIP", "").split(",") if s.strip()
    }
    version = stack_version()
    force = os.environ.get("DFE_PROOF_FORCE", "").strip().lower() in TRUTHY
    if version and not skip and not force and read_marker(PROVEN_MARKER) == version:
        log(f"SKIP {version} is already proven; DFE_PROOF_FORCE=1 runs it again")
        return 0
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
    passed = bool(results) and all(results.values())
    if passed and version and not skip:
        PROVEN_MARKER.write_text(f"{version}\n", encoding="utf-8", newline="\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
