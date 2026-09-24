#  Project:      dfe-docker
#  File:         tests/test_e2e_config_mounts.py
#  Purpose:      Assert the e2e suite runs each service on the config it mounts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The e2e suite's services read the config the test mounts, whatever make exports.

`make test-e2e` and `make test-resilience` include `.profile.mk`, which exports
the engine-rendered config paths, and compose runs each service on
`--config ${VAR:-<mount>}`. A text read of the scripts where a check needs one:
these tests run with no PyYAML, and test_e2e.py cannot be imported without it.
"""

import re

import pytest

import _common
import resolve_profile
from _common import COMPOSE_FILE, REPO_ROOT, SERVICE_TO_RENDERED_CONFIG_VAR

_RENDERED_VARS = set(SERVICE_TO_RENDERED_CONFIG_VAR.values())


def test_every_rendered_config_variable_is_blanked() -> None:
    environ = {
        var: f"/etc/dfe/apps/{service}/config.yaml"
        for service, var in SERVICE_TO_RENDERED_CONFIG_VAR.items()
    }
    environ["CLICKHOUSE_URL"] = "http://localhost:8123"

    _common._use_mounted_configs(environ=environ)

    assert {var: environ[var] for var in _RENDERED_VARS} == dict.fromkeys(
        _RENDERED_VARS, ""
    )
    assert environ["CLICKHOUSE_URL"] == "http://localhost:8123"


def test_a_variable_the_environment_never_had_is_still_set_empty() -> None:
    # Set-but-empty is what beats a value in .env; left unset, .env would win.
    environ: dict[str, str] = {}

    _common._use_mounted_configs(environ=environ)

    assert environ == dict.fromkeys(_RENDERED_VARS, "")


def test_every_config_variable_compose_reads_is_in_the_one_list() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    read = set(re.findall(r'"--config", "\$\{([A-Z0-9_]+):-', compose))

    assert read == _RENDERED_VARS


def test_the_resolver_and_the_suite_share_one_list() -> None:
    assert (
        resolve_profile.SERVICE_TO_RENDERED_CONFIG_VAR is SERVICE_TO_RENDERED_CONFIG_VAR
    )
    for script in ("resolve_profile.py", "test_e2e.py"):
        body = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert '_CONFIG_FILE"' not in body, (
            f"{script} names a rendered-config variable of its own"
        )


def test_the_suite_blanks_the_list_before_it_starts_anything() -> None:
    suite = (REPO_ROOT / "scripts" / "test_e2e.py").read_text(encoding="utf-8")

    assert re.search(r"^_use_mounted_configs\(environ=os\.environ\)$", suite, re.M)


@pytest.mark.parametrize(
    "args,named",
    [
        (["--config", "/etc/dfe/loader.yaml"], "/etc/dfe/loader.yaml"),
        (
            ["--log-level", "info", "--config=/etc/dfe/apps/dfe-loader/loader.yaml"],
            "/etc/dfe/apps/dfe-loader/loader.yaml",
        ),
        (["--config"], None),
        ([], None),
    ],
)
def test_the_config_a_process_runs_is_read_off_its_arguments(
    args: list[str], named: str | None
) -> None:
    assert _common._config_argument(args=args) == named
