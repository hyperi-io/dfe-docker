#!/usr/bin/env python3

#  Project:   dfe-docker
#  File:      scripts/test_source.py
#  Purpose:   Run dfe-infra's post-deploy source acceptance runner against this compose stack
#  Language:  Python
#
#  License:   BUSL-1.1
#  Copyright: (c) 2026 HYPERI PTY LIMITED
#
#  Usage:
#    ./scripts/test_source.py                                   # the filebeat case
#    ./scripts/test_source.py --case cloudwatch --aws-service cloudtrail
#    ./scripts/test_source.py --case elastic                    # dfe-transform-elastic
#    ./scripts/test_source.py --case vector                     # dfe-transform-vector
#    ./scripts/test_source.py -- --keep --per-module 5          # flags for the runner
#    DFE_INFRA_DIR=../dfe-infra ./scripts/test_source.py

"""The post-deploy source test, on the compose stack, over its published ports.

An operator's first real act on a working deployment is to add a source and see
data land. The runner that drives that -- the console in a browser, the engine
API, the datastore, the archive -- lives in dfe-infra
(``scripts/acceptance/source/run.py``) and is the same one the Kubernetes lanes
call through ``dfe-ops acceptance --suite source``. A copy here would be a
second definition of what the test IS, and the two would answer differently the
first time a step changed.

What this repo owns is the wiring: which ports the stack publishes, which
container the archive assertion runs inside, and where this deployment's admin
login comes from. The Kubernetes lane reads those from the cluster; here they
come from ``.env``, which is this deployment's own record of them.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from _common import _external_origin, _load_dotenv, _print
from _pipeline import env_or

PROJECT_DIR = Path(__file__).resolve().parent.parent
# Container names are daemon-wide and DFE_CONTAINER_PREFIX moves them, so the
# archiver and every app the runner restarts are reached through compose.
ARCHIVER_SERVICE = "dfe-archiver"
# The runner appends the compose service key the engine names to this prefix.
RESTART_EXEC = "docker compose --profile * restart --no-deps"
RUNNER_PATH = Path("scripts") / "acceptance" / "source" / "run.py"
# The cases that push a corpus, and the checkout the runner reads it out of.
# One repo for all three: they feed the same corpus and the elastic case takes
# its cisco_ios module out of it. The vector case's program comes from the
# dfe-transform-vector checkout beside the engine repo, which the runner finds
# itself. A case absent here passes no --transform-repo, which is the fetched
# cloudwatch one.
CORPUS_CASES = ("filebeat", "elastic", "vector")
CORPUS_REPO_ENV_VAR = "DFE_TRANSFORM_VRL_REPO"
CORPUS_MARKER = Path("pipelines") / "filebeat" / "filebeat.vrl"


def _infra_repo(flag: str | None) -> Path:
    """The dfe-infra checkout holding the runner, named explicitly.

    Same explicit-local-only rule as ``make check-profiles``: nothing here probes
    for a sibling directory it was not told about.
    """
    candidate = flag or os.environ.get("DFE_INFRA_DIR")
    if not candidate:
        sys.exit(
            "test_source: the runner lives in dfe-infra; point at a checkout with "
            "--infra-repo or DFE_INFRA_DIR"
        )
    repo = Path(candidate).expanduser().resolve()
    if not ((repo / RUNNER_PATH).is_file()):
        sys.exit(f"test_source: {repo} carries no {RUNNER_PATH}")
    return repo


def _engine_repo(flag: str | None) -> Path:
    """Where the corpus wrapper lives, without hardcoding one developer's tree."""
    candidate = flag or os.environ.get("DFE_ENGINE_REPO")
    if not candidate:
        sibling = PROJECT_DIR.parent / "dfe-engine"
        if not (sibling.is_dir()):
            sys.exit(
                "test_source: the corpus wrapper lives in dfe-engine; point at a "
                "checkout with --engine-repo or DFE_ENGINE_REPO"
            )
        candidate = str(sibling)
    repo = Path(candidate).expanduser().resolve()
    if not ((repo / "tests" / "e2e" / "filebeat_corpus.py").is_file()):
        sys.exit(f"test_source: {repo} carries no tests/e2e/filebeat_corpus.py")
    return repo


def _transform_repo(flag: str | None, case: str) -> Path | None:
    """The checkout this case's corpus is read out of.

    Optional: a fetched case reads none, and the runner falls back to the
    checkout beside the engine repo when this is not passed.
    """
    if case not in CORPUS_CASES:
        return None
    candidate = flag or os.environ.get(CORPUS_REPO_ENV_VAR)
    if not candidate:
        return None
    repo = Path(candidate).expanduser().resolve()
    if not ((repo / CORPUS_MARKER).is_file()):
        sys.exit(f"test_source: {repo} carries no {CORPUS_MARKER}")
    return repo


def _runner_python(flag: str | None) -> str:
    """An interpreter carrying Playwright, which the runner drives a browser with.

    Defaults to the one running this script; a machine whose system python is
    externally managed points at its own virtualenv instead.
    """
    python = flag or os.environ.get("DFE_ACCEPTANCE_PYTHON") or sys.executable
    probe = subprocess.run(
        [python, "-c", "import playwright"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        sys.exit(
            f"test_source: {python} has no playwright -- install it with "
            "`python3 -m pip install -r scripts/acceptance/requirements.txt && "
            "python3 -m playwright install chrome` in dfe-infra, and name that "
            "interpreter with --python or DFE_ACCEPTANCE_PYTHON"
        )
    return python


def _archive_exec() -> list[str]:
    """The command prefix that runs a shell inside the archiver, or nothing.

    One replica, because compose runs one archiver; the runner reports the
    archive step as skipped when the profile deploys none.
    """
    running = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "*",
            "ps",
            "--status",
            "running",
            "--quiet",
            ARCHIVER_SERVICE,
        ],
        capture_output=True,
        check=False,
        cwd=PROJECT_DIR,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ids = running.stdout.split()
    if running.returncode != 0 or not ids:
        return []
    return ["--archive-exec", f"docker exec {ids[0]}"]


def _ui_host(*, bound: str, host: str) -> str:
    """The address the console and the engine API answer on.

    The UI ports carry their own audience: the Makefile resolves DFE_BIND_SCOPE
    into DFE_UI_BIND_HOST and exports it, so they can sit on a different address
    from ingest and the backing services. A scope of `all` binds 0.0.0.0, which is
    what a server listens on rather than what a client dials, so that falls back
    to the address the rest of the stack is reached at.
    """
    scope = os.environ.get("DFE_BIND_SCOPE", "localhost").strip()
    resolved = bound or ("127.0.0.1" if scope == "localhost" else "0.0.0.0")
    return host if resolved == "0.0.0.0" else resolved


def _ui_origin(*, bound: str, host: str) -> str:
    """The scheme and host the console and the engine API answer on.

    DFE_EXTERNAL_ORIGIN is the address this deployment hands to browsers, so the
    suite drives the console there: the URL testers use is the one worth testing,
    and it is the one the stack builds its own absolute URLs from. It falls back
    to the published address where no such origin is set.
    """
    return (
        _external_origin(values=os.environ)
        or f"http://{_ui_host(bound=bound, host=host)}"
    )


def _suite_env(*, host: str, engine_url: str) -> dict[str, str]:
    """The DFE_E2E_* block, the same one dfe-ops exports for the Kubernetes lanes.

    There it comes from the cluster's secrets and port-forwards; here from .env
    and the published ports, which is where a compose deployment keeps them.
    """
    password = os.environ.get("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "")
    if not (password):
        sys.exit(
            "test_source: the runner signs in as this deployment's admin, so it "
            "needs DFE_AUTH_LOCAL_ADMIN_PASSWORD from this stack's .env"
        )
    env = dict(os.environ)
    env.update(
        {
            "DFE_E2E_RECEIVER_URL": env_or(
                "DFE_RECEIVER_INGEST_URL",
                f"http://{host}:{env_or('DFE_RECEIVER_HTTP_PORT', '8080')}/ingest",
            ),
            "DFE_E2E_CH_HOST": host,
            "DFE_E2E_CH_PORT": env_or("CLICKHOUSE_HTTP_PORT", "8123"),
            "DFE_E2E_CH_USER": env_or("CLICKHOUSE_USERNAME", "default"),
            "DFE_E2E_CH_PASSWORD": os.environ.get("CLICKHOUSE_PASSWORD", ""),
            # The data-path database the loader writes to (config/loader/*.yaml
            # all pin `database: dfe`), never DFE_OTEL_DATABASE's.
            "DFE_E2E_CH_DB": env_or("DFE_E2E_CH_DB", "dfe"),
            "DFE_E2E_ENGINE_URL": engine_url,
            "DFE_E2E_ENGINE_USER": env_or("DFE_AUTH_LOCAL_ADMIN_NAME", "admin"),
            "DFE_E2E_ENGINE_PASSWORD": password,
            "DFE_E2E_ADMIN_PASSWORD": password,
        }
    )
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="test_source.py",
        description="Run dfe-infra's post-deploy source acceptance runner against this compose stack.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--infra-repo", default=None, help="dfe-infra checkout holding the runner"
    )
    parser.add_argument(
        "--engine-repo",
        default=None,
        help="dfe-engine checkout the corpus wrapper lives in",
    )
    parser.add_argument(
        "--transform-repo",
        default=None,
        help="dfe-transform-vrl checkout holding the corpus the pushed cases feed",
    )
    parser.add_argument(
        "--python", default=None, help="interpreter carrying Playwright"
    )
    parser.add_argument(
        "--case",
        default="filebeat",
        choices=("filebeat", "cloudwatch", "elastic", "vector"),
        help="filebeat, elastic and vector push real lines at the receiver, through "
        "dfe-transform-vrl, dfe-transform-elastic and dfe-transform-vector; "
        "cloudwatch lets a fetcher pull an AWS upstream",
    )
    parser.add_argument(
        "--aws-service",
        default=None,
        help="the AWS service the cloudwatch case's fetcher polls (runner default: cloudwatch_logs)",
    )
    parser.add_argument(
        "--shots-dir", default=".tmp/source", help="where the per-step screenshots go"
    )
    # Anything this wrapper does not own goes to the runner untouched, so its own
    # flags (--keep, --per-module, --headed) need no twin here.
    args, runner_args = parser.parse_known_args(argv)

    # Read before .env is merged: the Makefile exports the address it bound the UI
    # ports to, and .env's own copy is inert by design (.env.example says not to
    # set it by hand, because a raw `docker compose` gets the loopback default).
    ui_bind = os.environ.get("DFE_UI_BIND_HOST", "").strip()
    _load_dotenv()
    infra = _infra_repo(args.infra_repo)
    engine = _engine_repo(args.engine_repo)
    transform = _transform_repo(args.transform_repo, args.case)
    python = _runner_python(args.python)

    # The address the stack publishes on, which is `localhost` for one stack on a
    # box and something else for a second one beside it. The same variable
    # `make post` reads, so the two runners reach the same containers.
    host = env_or("DFE_POST_HOST", "localhost")
    ui_origin = _ui_origin(bound=ui_bind, host=host)
    ui_url = f"{ui_origin}:{env_or('DFE_UI_PORT', '3000')}"
    engine_url = f"{ui_origin}:{env_or('DFE_ENGINE_PORT', '8003')}"

    runner = [
        python,
        str(infra / RUNNER_PATH),
        "--ui-url",
        ui_url,
        # No gateway here, so the console and the API are two ports on one host
        # rather than one hostname; the runner takes them separately for this.
        "--engine-url",
        engine_url,
        "--engine-repo",
        str(engine),
        "--shots-dir",
        args.shots_dir,
        "--case",
        args.case,
    ]
    if args.aws_service:
        runner += ["--aws-service", args.aws_service]
    if transform:
        runner += ["--transform-repo", str(transform)]
    # The upstream a fetched case polls is a property of the DEPLOYMENT, so it
    # comes from .env rather than from this script.
    for flag, key in (
        ("--aws-region", "DFE_AWS_REGION"),
        ("--aws-log-group", "DFE_AWS_LOG_GROUP"),
    ):
        if os.environ.get(key):
            runner += [flag, os.environ[key]]
    runner += _archive_exec()
    # No controller here restarts an app after a config write, which is why the
    # flag is passed at all.
    runner += ["--restart-exec", RESTART_EXEC]
    runner += [arg for arg in runner_args if arg != "--"]

    _print(msg=f"case {args.case}, console {ui_url}, runner from {infra}")
    return subprocess.run(
        runner, env=_suite_env(host=host, engine_url=engine_url)
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
