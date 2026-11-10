#  Project:      dfe-docker
#  File:         scripts/tests/test_hyperdx_embed_exposure.py
#  Purpose:      Assert the exposure dials and port keys govern what make publishes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The console frames dfe-hyperdx from dfe-proxy's embed port, so dfe-hyperdx's dials cover it.

The embed stays product class and outside the auth profile's gate, because an OIDC
redirect cannot complete inside a frame. The dfe-hyperdx kill switches take it dark with
the dfe-hyperdx origins. Each case runs a copy of the Makefile for the chain it resolves,
then reads the real compose model through that chain. The receiver's optional listeners go the same way, each published by its own port key.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import check_compose

REPO_ROOT = Path(__file__).resolve().parents[2]

# Container ports dfe-proxy publishes: the console and the dfe-hyperdx embed.
_CONSOLE = "8080"
_EMBED = "8091"
# Container ports dfe-receiver publishes whatever its port keys say.
_RECEIVER_BASE = {"8080", "9090"}
_RECEIVER_FRAGMENT = ".tmp/docker-compose.receiver-ports.yml"

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


def _chained_fragment(
    *, checkout: Path, name: str, settings: dict[str, str]
) -> str | None:
    """The fragment `name` as `make print-compose-file` chains it for `settings`, None when it chains none."""
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
    if name not in line.removeprefix("COMPOSE_FILE=").split(":"):
        return None
    return (checkout / name).read_text(encoding="utf-8")


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
    fragment = _chained_fragment(
        checkout=checkout, name=check_compose._UNPUBLISH_FRAGMENT, settings=settings
    )
    environ, _ = check_compose._check_env()

    model = check_compose._config_json(
        env=environ, files=[check_compose.COMPOSE_FILE.name], fragment=fragment
    )

    assert model is not None
    targets = {
        str(port.get("target"))
        for port in model["services"]["dfe-proxy"].get("ports") or []
    }
    assert (_CONSOLE in targets) == console
    assert (_EMBED in targets) == embed


@pytest.mark.parametrize(
    ("settings", "published"),
    [
        pytest.param({}, set(), id="none-set"),
        pytest.param(
            {"DFE_RECEIVER_BEATS_PORT": "15044"},
            {("15044", "5044")},
            id="one-on-its-own-host-port",
        ),
        pytest.param(
            {
                "DFE_RECEIVER_BEATS_PORT": "5044",
                "DFE_RECEIVER_GRPC_PORT": "6000",
                "DFE_RECEIVER_HEC_PORT": "8088",
                "DFE_RECEIVER_OTLP_GRPC_PORT": "4317",
                "DFE_RECEIVER_OTLP_HTTP_PORT": "4318",
            },
            {
                ("4317", "4317"),
                ("4318", "4318"),
                ("5044", "5044"),
                ("6000", "6000"),
                ("8088", "8088"),
            },
            id="all",
        ),
    ],
)
def test_a_receiver_port_key_publishes_its_listener(
    checkout: Path, settings: dict[str, str], published: set[tuple[str, str]]
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("`docker compose config` renders the model this asserts on")
    fragment = _chained_fragment(
        checkout=checkout, name=_RECEIVER_FRAGMENT, settings=settings
    )
    environ, _ = check_compose._check_env()

    assert (fragment is None) == (not (published))
    model = check_compose._config_json(
        env={**environ, **settings},
        files=[check_compose.COMPOSE_FILE.name],
        fragment=fragment,
    )

    assert model is not None
    ports = model["services"]["dfe-receiver"].get("ports") or []
    assert {
        (str(port.get("published")), str(port.get("target")))
        for port in ports
        if str(port.get("target")) not in _RECEIVER_BASE
    } == published
