#!/usr/bin/env python3

#  Project:   dfe-docker
#  File:      scripts/test_flows.py
#  Purpose:   Run the engine's flow e2e suite against this compose stack
#  Language:  Python
#
#  License:   BUSL-1.1
#  Copyright: (c) 2026 HYPERI PTY LIMITED
#
#  Usage:
#    ./scripts/test_flows.py                        # the active profile's transport
#    ./scripts/test_flows.py --profile single       # a named service_profiles.yaml profile
#    ./scripts/test_flows.py --engine-repo ../dfe-engine
#    DFE_ENGINE_REPO=../dfe-engine ./scripts/test_flows.py

"""The flow shapes, on the compose stack, over localhost ports.

The fixtures and the assertions live in dfe-engine (``tests/e2e/flows``) and are
run from a checkout of it, exactly as dfe-ops does for the Kubernetes lanes. A
copy of them here would be a second definition of what a flow shape IS, and the
two would answer differently the first time a shape changed.

What this repo owns is the wiring: which ports the stack publishes and which
transport the profile runs. The transport comes from ``service_profiles.yaml``,
which is where every other tool here reads it from.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml

from _common import _load_dotenv
from _pipeline import env_or

PROJECT_DIR = Path(__file__).resolve().parent.parent
SERVICE_PROFILES_FILE = PROJECT_DIR / "service_profiles.yaml"


def _profiles() -> dict:
    with open(SERVICE_PROFILES_FILE, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _transport(profile_name: str) -> str:
    """The data path the named profile runs, in the words the suite expects.

    Same words on both sides: service_profiles.yaml already says grpc or kafka,
    and so does DFE_E2E_TRANSPORT.
    """
    data = _profiles()
    profiles = data.get("profiles") or {}
    if profile_name not in profiles:
        sys.exit(
            f"test_flows: profile '{profile_name}' is not in service_profiles.yaml "
            f"(have: {', '.join(sorted(profiles))})"
        )
    transport = (profiles[profile_name] or {}).get("transport", "")
    if transport not in ("grpc", "kafka"):
        sys.exit(
            f"test_flows: profile '{profile_name}' declares transport '{transport}'"
        )
    return transport


def _engine_repo(flag: str | None) -> Path:
    """Where the flow suite lives, without hardcoding one developer's tree."""
    candidate = flag or os.environ.get("DFE_ENGINE_REPO")
    if not candidate:
        sibling = PROJECT_DIR.parent / "dfe-engine"
        if not sibling.is_dir():
            sys.exit(
                "test_flows: the fixtures and assertions live in dfe-engine; point at a "
                "checkout with --engine-repo or DFE_ENGINE_REPO"
            )
        candidate = str(sibling)
    repo = Path(candidate).resolve()
    if not (repo / "tests" / "e2e" / "flows").is_dir():
        sys.exit(f"test_flows: {repo} carries no tests/e2e/flows")
    return repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="test_flows.py",
        description="Run dfe-engine's flow e2e suite against this compose stack.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="service_profiles.yaml profile the stack is running (default: DFE_PROFILE, else active_profile)",
    )
    parser.add_argument(
        "--engine-repo", default=None, help="dfe-engine checkout the suite runs from"
    )
    parser.add_argument(
        "--transport",
        default=None,
        choices=("grpc", "kafka", "both"),
        help="override the profile's data path; both fails on a stack that cannot carry one",
    )
    parser.add_argument(
        "pytest_args", nargs=argparse.REMAINDER, help="passed through to pytest"
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    profile = (
        args.profile
        or os.environ.get("DFE_PROFILE")
        or _profiles().get("active_profile")
    )
    if not profile:
        sys.exit(
            "test_flows: no profile named and service_profiles.yaml sets no active_profile"
        )
    transport = args.transport or _transport(profile)
    repo = _engine_repo(args.engine_repo)

    # The address the stack publishes on, which is `localhost` for one stack on a
    # box and something else for a second one beside it. The same variable
    # `make post` reads, so the two runners reach the same containers.
    host = os.environ.get("DFE_POST_HOST", "localhost")
    env = dict(os.environ)
    env.update(
        {
            "DFE_E2E_RECEIVER_URL": os.environ.get(
                "DFE_RECEIVER_INGEST_URL",
                f"http://{host}:{os.environ.get('DFE_RECEIVER_HTTP_PORT', '8080')}/ingest",
            ),
            "DFE_E2E_CH_HOST": host,
            "DFE_E2E_CH_PORT": env_or(
                "CLICKHOUSE_HTTP_HOST_PORT", env_or("CLICKHOUSE_HTTP_PORT", "8123")
            ),
            "DFE_E2E_CH_USER": os.environ.get("CLICKHOUSE_USERNAME", "default"),
            "DFE_E2E_CH_PASSWORD": os.environ.get("CLICKHOUSE_PASSWORD", ""),
            "DFE_E2E_CH_DB": os.environ.get("DFE_OTEL_DATABASE", "dfe"),
            "DFE_E2E_ENGINE_URL": f"http://{host}:{os.environ.get('DFE_ENGINE_PORT', '8003')}",
            "DFE_E2E_ENGINE_USER": os.environ.get("DFE_AUTH_LOCAL_ADMIN_NAME", "admin"),
            "DFE_E2E_TRANSPORT": transport,
        }
    )
    password = os.environ.get("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "")
    if not password:
        sys.exit(
            "test_flows: the suite writes sources through the engine, so it needs "
            "DFE_AUTH_LOCAL_ADMIN_PASSWORD from this stack's .env"
        )
    env["DFE_E2E_ENGINE_PASSWORD"] = password

    print(
        f"==> profile {profile}, transport {transport}, suite from {repo}",
        file=sys.stderr,
    )
    # Serial: every source the suite writes rolls the receiver, so a parallel
    # worker would post into a container that is restarting.
    pytest_args = args.pytest_args or [
        "tests/e2e/flows",
        "-m",
        "live",
        "-v",
        "--no-cov",
        "-p",
        "no:randomly",
        "-n",
        "0",
    ]
    return subprocess.run(
        ["uv", "run", "pytest", *pytest_args], cwd=repo, env=env
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
