#  Project:      dfe-docker
#  File:         tests/test_proxy_tls_guard.py
#  Purpose:      Assert DFE_PROXY_TLS refuses to start without what console TLS needs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The console TLS prechecks, driven through make itself.

Most of them are parse-time `$(error)`s, reachable from a copy of the Makefile in a directory of its own as in test_origin_guard.py. The auth refusal runs in the .profile.mk recipe once the resolver has written this run's answer, so the copy gets a stub resolver that writes the footprint a test asks for. It also gets the real helper the Makefile runs while it parses, a real throwaway certificate pair and a stub `docker` that reports the networks a test writes, so no daemon decides an answer.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import proxy_tls

REPO_ROOT = Path(__file__).resolve().parents[2]

# The sentences each guard is recognised by, quoted from the Makefile and scripts/proxy_tls.py.
_AUTH = "cannot start with the auth profile"
_BARE_CERT_DIR = "must be a path starting with"
_E2E = "Turn DFE_PROXY_TLS off for e2e"
_HTTP_HYPERDX = "DFE_HYPERDX_APP_URL must start with https://"
_HTTP_ORIGIN = "DFE_EXTERNAL_ORIGIN must start with https://"
_MISSING_CERT = "needs the certificate ./certs/console.crt"
_MISSING_KEY = "needs the private key ./certs/console.key"
_NOT_A_DIR = "is not a directory"
_NOT_A_KEY = "is not an unencrypted PEM private key"
_OVERLAP = "overlaps Docker network"
_STALE_NETWORK = "Run `make down` first"
_SUBNET = "cannot hold the TLS network"
_SYMLINK = "is a symlink"
_TYPO = "DFE_PROXY_TLS must be `true` or `false`"
_UNREADABLE_KEY = "./certs/console.key exists but"
_GUARDS = (
    _AUTH,
    _BARE_CERT_DIR,
    _E2E,
    _HTTP_HYPERDX,
    _HTTP_ORIGIN,
    _MISSING_CERT,
    _MISSING_KEY,
    _NOT_A_DIR,
    _NOT_A_KEY,
    _OVERLAP,
    _STALE_NETWORK,
    _SUBNET,
    _SYMLINK,
    _TYPO,
    _UNREADABLE_KEY,
)

# Every goal that starts or self-tests the stack, ORIGIN_GOALS in the Makefile.
_GUARDED_GOALS = [
    "dev",
    "ci",
    "up",
    "apply",
    "infra",
    "post",
    "test-source",
    "test-flows",
]
# The goals that recreate services on the live network with no `make down` first.
_REUSE_GOALS = ["apply", "apply-services", "infra"]
# Stopping a stack, checking a file and reading the chain must work whatever TLS needs.
_UNGUARDED_GOALS = [
    "down",
    "clean",
    "creds",
    "init",
    "help",
    "check-compose",
    "print-compose-file",
]

_PROJECT = "dfe-docker"
_TLS_ON = {
    "COMPOSE_PROJECT_NAME": _PROJECT,
    "DFE_EXTERNAL_ORIGIN": "https://dfe.example.test",
    "DFE_PROXY_TLS": "true",
}

# Answers `docker network ls -q` and `docker network inspect` from docker-networks.json in the working directory.
_STUB_DOCKER = """\
#!/usr/bin/env python3
import json
import sys
from pathlib import Path

source = Path("docker-networks.json")
networks = json.loads(source.read_text(encoding="utf-8")) if source.exists() else []
if sys.argv[1:4] == ["network", "ls", "-q"]:
    print("\\n".join(network["Id"] for network in networks))
elif sys.argv[1:3] == ["network", "inspect"]:
    print(json.dumps([network for network in networks if network["Id"] in sys.argv[3:]]))
else:
    sys.exit(1)
"""

# Writes only on a change, as the real resolver does: a FORCE target rewritten on every pass would restart make forever.
_STUB_RESOLVER = """\
from pathlib import Path

target = Path(".profile.mk")
text = "export DFE_AUTH_RESOLVED := {auth}\\n"
if (not (target.exists())) or (target.read_text(encoding="utf-8") != text):
    target.write_text(text, encoding="utf-8")
"""

# A goal the copy alone carries, to read back what the Makefile exports to compose.
_PRINT_TLS_ENV = """
.PHONY: print-tls-env
print-tls-env:
\t@echo "$$DFE_PROXY_IP $$DFE_NETWORK_IP_RANGE $$DFE_NETWORK_SUBNET $$DFE_PROXY_CERT_DIR"
"""


def _make(
    *, cwd: Path, dry_run: bool = True, goal: str, **overrides: str
) -> subprocess.CompletedProcess[str]:
    """Run one goal with the stub docker first on PATH and every DFE_* key, the project name and any outer make's flags dropped, then the overrides applied."""
    dropped = ("COMPOSE_PROJECT_NAME", "MAKEFLAGS", "MAKELEVEL", "MFLAGS")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not ((key.startswith("DFE_")) or (key in dropped))
    }
    environment["PATH"] = f"{cwd / 'bin'}{os.pathsep}{environment.get('PATH', '')}"
    environment.update(overrides)
    return subprocess.run(
        ["make", "--no-print-directory", *(["-n"] if dry_run else []), goal],
        capture_output=True,
        check=False,
        cwd=cwd,
        encoding="utf-8",
        env=environment,
        errors="replace",
        text=True,
    )


def _networks(*, cwd: Path, networks: list[tuple[str, str, str]]) -> None:
    """Give the stub docker these (name, subnet, ip_range) networks to report."""
    described = [
        {
            "IPAM": {"Config": [{"IPRange": rng, "Subnet": subnet}]},
            "Id": f"id{index}",
            "Name": name,
        }
        for index, (name, subnet, rng) in enumerate(networks)
    ]
    (cwd / "docker-networks.json").write_text(
        json.dumps(described), encoding="utf-8", newline="\n"
    )


def _resolve_auth(*, auth: bool, cwd: Path) -> None:
    """Point the copy's stub resolver at an auth footprint."""
    (cwd / "scripts" / "resolve_profile.py").write_text(
        _STUB_RESOLVER.format(auth="true" if auth else "false"),
        encoding="utf-8",
        newline="\n",
    )


@pytest.fixture
def makefile(tmp_path: Path) -> Path:
    """A copy of the Makefile with an empty .env, the TLS helper, a stub resolver, a stub docker and a real certificate pair."""
    shutil.copy(dst=tmp_path / "Makefile", src=REPO_ROOT / "Makefile")
    with (tmp_path / "Makefile").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_PRINT_TLS_ENV)
    (tmp_path / ".env").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "scripts").mkdir()
    shutil.copy(
        dst=tmp_path / "scripts" / "proxy_tls.py",
        src=REPO_ROOT / "scripts" / "proxy_tls.py",
    )
    _resolve_auth(auth=False, cwd=tmp_path)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "docker").write_text(
        _STUB_DOCKER, encoding="utf-8", newline="\n"
    )
    (tmp_path / "bin" / "docker").chmod(0o755)
    proxy_tls.throwaway_pair(directory=tmp_path / "certs")
    return tmp_path


@pytest.mark.parametrize("goal", _GUARDED_GOALS)
def test_a_prepared_start_passes_every_guard(goal: str, makefile: Path) -> None:
    result = _make(cwd=makefile, goal=goal, **_TLS_ON)

    assert result.returncode == 0, result.stderr
    assert not ([guard for guard in _GUARDS if guard in result.stderr])


@pytest.mark.parametrize("goal", _GUARDED_GOALS)
def test_a_missing_certificate_stops_the_start(goal: str, makefile: Path) -> None:
    (makefile / "certs" / "console.crt").unlink()

    result = _make(cwd=makefile, goal=goal, **_TLS_ON)

    assert result.returncode != 0
    assert _MISSING_CERT in result.stderr


def test_a_missing_key_stops_the_start(makefile: Path) -> None:
    (makefile / "certs" / "console.key").unlink()

    result = _make(cwd=makefile, goal="ci", **_TLS_ON)

    assert result.returncode != 0
    assert _MISSING_KEY in result.stderr


def test_an_empty_key_stops_the_start(makefile: Path) -> None:
    (makefile / "certs" / "console.key").write_text("", encoding="utf-8", newline="\n")

    result = _make(cwd=makefile, goal="ci", **_TLS_ON)

    assert result.returncode != 0
    assert _NOT_A_KEY in result.stderr


def test_a_symlinked_key_stops_the_start(makefile: Path) -> None:
    (makefile / "certs" / "console.key").rename(makefile / "real.key")
    (makefile / "certs" / "console.key").symlink_to(makefile / "real.key")

    result = _make(cwd=makefile, goal="ci", **_TLS_ON)

    assert result.returncode != 0
    assert _SYMLINK in result.stderr
    assert "cp -L" in result.stderr


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads a 0000 file, so nothing is unreadable"
)
def test_an_unreadable_key_is_named_as_unreadable_not_missing(makefile: Path) -> None:
    (makefile / "certs" / "console.key").chmod(0o000)

    result = _make(cwd=makefile, goal="ci", **_TLS_ON)

    assert result.returncode != 0
    assert _UNREADABLE_KEY in result.stderr
    assert _MISSING_KEY not in result.stderr


@pytest.mark.parametrize(
    ("cert_dir", "said"),
    [("certs", _BARE_CERT_DIR), ("./certs/console.crt", _NOT_A_DIR)],
)
def test_a_cert_dir_that_is_no_directory_stops_the_start(
    cert_dir: str, makefile: Path, said: str
) -> None:
    result = _make(
        cwd=makefile, goal="ci", **{**_TLS_ON, "DFE_PROXY_CERT_DIR": cert_dir}
    )

    assert result.returncode != 0
    assert said in result.stderr


@pytest.mark.parametrize("origin", ["", "http://dfe.example.test"])
def test_an_origin_that_is_not_https_stops_the_start(
    makefile: Path, origin: str
) -> None:
    result = _make(
        cwd=makefile, goal="ci", **{**_TLS_ON, "DFE_EXTERNAL_ORIGIN": origin}
    )

    assert result.returncode != 0
    assert _HTTP_ORIGIN in result.stderr


def test_a_schemeless_origin_under_tls_is_told_to_use_https(makefile: Path) -> None:
    result = _make(
        cwd=makefile,
        goal="ci",
        **{**_TLS_ON, "DFE_EXTERNAL_ORIGIN": "dfe.example.test"},
    )

    assert result.returncode != 0
    assert "Set DFE_EXTERNAL_ORIGIN=https://dfe.example.test" in result.stderr


@pytest.mark.parametrize(
    ("url", "refused"),
    [("http://hyperdx.example.test", True), ("https://hyperdx.example.test", False)],
)
def test_a_hyperdx_url_must_be_https_when_set(
    makefile: Path, url: str, refused: bool
) -> None:
    result = _make(cwd=makefile, goal="ci", **{**_TLS_ON, "DFE_HYPERDX_APP_URL": url})

    assert (_HTTP_HYPERDX in result.stderr) == refused
    assert (result.returncode != 0) == refused


@pytest.mark.parametrize("goal", _GUARDED_GOALS)
def test_the_auth_profile_stops_the_start(goal: str, makefile: Path) -> None:
    _resolve_auth(auth=True, cwd=makefile)

    result = _make(cwd=makefile, goal=goal, **_TLS_ON)

    assert result.returncode != 0
    assert _AUTH in result.stderr


def test_the_auth_refusal_reads_this_runs_resolution_not_the_last(
    makefile: Path,
) -> None:
    (makefile / ".profile.mk").write_text(
        "export DFE_AUTH_RESOLVED := true\n", encoding="utf-8", newline="\n"
    )

    result = _make(cwd=makefile, goal="ci", **_TLS_ON)

    assert result.returncode == 0, result.stderr
    assert _AUTH not in result.stderr


@pytest.mark.parametrize(
    "subnet", ["172.16.1.5/24", "172.16.1.0/26", "10.0.0.0/8", "8.8.8.0/24", "bogus"]
)
def test_a_subnet_that_cannot_hold_the_network_stops_the_start(
    makefile: Path, subnet: str
) -> None:
    result = _make(cwd=makefile, goal="ci", **{**_TLS_ON, "DFE_NETWORK_SUBNET": subnet})

    assert result.returncode != 0
    assert _SUBNET in result.stderr
    assert f"DFE_NETWORK_SUBNET={subnet!r}" in result.stderr


def test_a_subnet_another_network_holds_stops_the_start(makefile: Path) -> None:
    _networks(cwd=makefile, networks=[("other_default", "10.207.0.0/16", "")])

    result = _make(cwd=makefile, goal="ci", **_TLS_ON)

    assert result.returncode != 0
    assert _OVERLAP in result.stderr
    assert "DFE_NETWORK_SUBNET='10.207.0.0/24'" in result.stderr


@pytest.mark.parametrize("goal", _REUSE_GOALS)
@pytest.mark.parametrize(
    ("tls", "pool", "refused"),
    [
        ("true", ("172.16.1.0/24", ""), True),
        ("true", ("10.207.0.0/24", "10.207.0.0/25"), False),
        ("false", ("10.207.0.0/24", "10.207.0.0/25"), True),
        ("false", ("172.16.1.0/24", ""), False),
    ],
)
def test_a_dial_flip_on_a_live_network_needs_a_down_first(
    makefile: Path, goal: str, tls: str, pool: tuple[str, str], refused: bool
) -> None:
    _networks(cwd=makefile, networks=[(f"{_PROJECT}_default", *pool)])

    result = _make(cwd=makefile, goal=goal, **{**_TLS_ON, "DFE_PROXY_TLS": tls})

    assert (_STALE_NETWORK in result.stderr) == refused
    assert (result.returncode != 0) == refused


@pytest.mark.parametrize("goal", ["dev", "ci", "up"])
def test_goals_that_run_down_first_may_meet_the_old_network(
    goal: str, makefile: Path
) -> None:
    _networks(cwd=makefile, networks=[(f"{_PROJECT}_default", "172.16.1.0/24", "")])

    result = _make(cwd=makefile, goal=goal, **_TLS_ON)

    assert result.returncode == 0, result.stderr
    assert _STALE_NETWORK not in result.stderr


@pytest.mark.parametrize("goal", ["test-e2e", "test-resilience"])
def test_the_e2e_suite_refuses_console_tls(goal: str, makefile: Path) -> None:
    result = _make(cwd=makefile, goal=goal, **_TLS_ON)

    assert result.returncode != 0
    assert _E2E in result.stderr


@pytest.mark.parametrize("goal", ["ci", "down", "help"])
@pytest.mark.parametrize("value", ["yes", "1", "True", "TRUE", "true false"])
def test_anything_but_true_or_false_fails_every_goal(
    goal: str, makefile: Path, value: str
) -> None:
    result = _make(cwd=makefile, goal=goal, DFE_PROXY_TLS=value)

    assert result.returncode != 0
    assert _TYPO in result.stderr


def test_an_empty_dial_reads_as_false(makefile: Path) -> None:
    shutil.rmtree(makefile / "certs")

    result = _make(
        cwd=makefile,
        goal="ci",
        DFE_EXTERNAL_ORIGIN="http://dfe.example.test",
        DFE_PROXY_TLS="",
    )
    chain = _make(cwd=makefile, goal="print-compose-file", DFE_PROXY_TLS="")

    assert result.returncode == 0, result.stderr
    assert not ([guard for guard in _GUARDS if guard in result.stderr])
    assert "docker-compose.tls.yml" not in chain.stdout


@pytest.mark.parametrize("goal", _UNGUARDED_GOALS)
def test_stopping_and_checking_never_need_what_tls_needs(
    goal: str, makefile: Path
) -> None:
    shutil.rmtree(makefile / "certs")
    _resolve_auth(auth=True, cwd=makefile)
    _networks(cwd=makefile, networks=[(f"{_PROJECT}_default", "172.16.1.0/24", "")])

    result = _make(
        cwd=makefile,
        goal=goal,
        COMPOSE_PROJECT_NAME=_PROJECT,
        DFE_EXTERNAL_ORIGIN="http://dfe.example.test",
        DFE_PROXY_TLS="true",
    )

    assert result.returncode == 0, result.stderr
    assert not ([guard for guard in _GUARDS if guard in result.stderr])


def test_with_the_dial_off_nothing_is_needed(makefile: Path) -> None:
    shutil.rmtree(makefile / "certs")
    _resolve_auth(auth=True, cwd=makefile)

    result = _make(
        cwd=makefile, goal="ci", DFE_EXTERNAL_ORIGIN="http://dfe.example.test"
    )

    assert result.returncode == 0, result.stderr
    assert not ([guard for guard in _GUARDS if guard in result.stderr])


@pytest.mark.parametrize(("tls", "chained"), [("true", True), ("false", False)])
def test_the_dial_chains_the_tls_overlay(
    chained: bool, makefile: Path, tls: str
) -> None:
    result = _make(cwd=makefile, goal="print-compose-file", DFE_PROXY_TLS=tls)

    assert ("docker-compose.tls.yml" in result.stdout) == chained


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "10.207.0.254 10.207.0.0/25 10.207.0.0/24 ./certs"),
        (
            {
                "DFE_NETWORK_SUBNET": "10.20.30.0/24",
                "DFE_PROXY_CERT_DIR": "/srv/dfe/certs",
            },
            "10.20.30.254 10.20.30.0/25 10.20.30.0/24 /srv/dfe/certs",
        ),
    ],
)
def test_compose_gets_the_derived_network(
    expected: str, makefile: Path, overrides: dict[str, str]
) -> None:
    result = _make(
        cwd=makefile,
        dry_run=False,
        goal="print-tls-env",
        DFE_PROXY_TLS="true",
        **overrides,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
