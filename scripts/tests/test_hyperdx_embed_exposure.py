#  Project:      dfe-docker
#  File:         scripts/tests/test_hyperdx_embed_exposure.py
#  Purpose:      Assert the HyperDX exposure dials govern the console's HyperDX embed port
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The console frames HyperDX from dfe-proxy's embed port, so HyperDX's dials cover it.

The embed stays product class and outside the auth profile's gate, because an OIDC
redirect cannot complete inside a frame. The HyperDX kill switches take it dark with
the HyperDX origins. Each case runs a copy of the Makefile for the chain it resolves,
then reads the real compose model through that chain.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import check_compose

REPO_ROOT = Path(__file__).resolve().parents[2]

# Container ports dfe-proxy publishes: the console and the HyperDX embed.
_CONSOLE = "8080"
_EMBED = "8091"

_RESOLVER = '''\
import os
from pathlib import Path

target = Path(".tmp/profile.mk")
content = f"""\\
export PROFILE_FLAGS := --profile core
export DFE_SERVICES := dfe-engine dfe-ui dfe-proxy
export DFE_BUILDABLE_SERVICES := dfe-engine dfe-ui
export DFE_INSTANCES_RESOLVED := false
export DFE_AUTH_RESOLVED := {os.environ.get("STUB_AUTH", "false")}
export DFE_OTEL_RESOLVED := true
"""
if not target.is_file() or target.read_text(encoding="utf-8") != content:
    target.parent.mkdir(exist_ok=True, parents=True)
    target.write_text(content, encoding="utf-8")
'''


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """The Makefile and a stand-in resolver, with no .env."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(dst=root / "Makefile", src=REPO_ROOT / "Makefile")
    shutil.copy(
        dst=root / "scripts" / "proxy_tls.py",
        src=REPO_ROOT / "scripts" / "proxy_tls.py",
    )
    (root / "scripts" / "resolve_profile.py").write_text(
        data=_RESOLVER, encoding="utf-8", newline="\n"
    )
    return root


def _ui_chain(*, checkout: Path, settings: dict[str, str]) -> list[str]:
    """The exposure fragments `make print-compose-file` chains for `settings`."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("DFE_"))
        and key not in ("COMPOSE_FILE", "MAKEFLAGS", "MAKELEVEL", "MFLAGS")
    }
    result = subprocess.run(
        args=["make", "--no-print-directory", "print-compose-file"],
        capture_output=True,
        check=False,
        cwd=checkout,
        encoding="utf-8",
        env={**environment, **settings},
        errors="replace",
        stdin=subprocess.DEVNULL,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("COMPOSE_FILE=")
    )
    files = line.removeprefix("COMPOSE_FILE=").split(":")
    return [name for name in files if name.startswith("docker-compose.unpublish-")]


@pytest.mark.parametrize(
    ("settings", "console", "embed"),
    [
        pytest.param({}, True, True, id="default"),
        pytest.param(
            {"DFE_INFRA_UIS_EXTERNAL": "false"}, True, False, id="kill-switch"
        ),
        pytest.param(
            {"DFE_HYPERDX_UI_EXTERNAL": "false"}, True, False, id="hyperdx-off"
        ),
        pytest.param({"STUB_AUTH": "true"}, True, True, id="auth-armed"),
        pytest.param(
            {"STUB_AUTH": "true", "DFE_INFRA_UIS_EXTERNAL": "false"},
            True,
            False,
            id="auth-armed-kill-switch",
        ),
        pytest.param(
            {"DFE_UI_EXTERNAL": "false", "DFE_INFRA_UIS_EXTERNAL": "false"},
            False,
            False,
            id="console-off",
        ),
        pytest.param({"DFE_KAFBAT_UI_EXTERNAL": "false"}, True, True, id="kafbat-off"),
    ],
)
def test_the_hyperdx_dials_govern_the_embed_and_leave_the_console(
    checkout: Path, settings: dict[str, str], console: bool, embed: bool
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("`docker compose config` renders the model this asserts on")
    fragments = _ui_chain(checkout=checkout, settings=settings)
    environ, _ = check_compose._check_env()

    model = check_compose._config_json(
        env=environ, files=[check_compose.COMPOSE_FILE.name, *fragments]
    )

    assert model is not None
    targets = {
        str(port.get("target"))
        for port in model["services"]["dfe-proxy"].get("ports") or []
    }
    assert (_CONSOLE in targets) == console
    assert (_EMBED in targets) == embed
