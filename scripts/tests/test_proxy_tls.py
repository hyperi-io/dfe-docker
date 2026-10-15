#  Project:      dfe-docker
#  File:         tests/test_proxy_tls.py
#  Purpose:      Assert the console TLS helper derives a safe network and refuses what would not start
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks scripts/proxy_tls.py runs for the Makefile, called directly.

Docker gives a network's gateway its first host address and allocates containers from the ip_range, so dfe-proxy's address must sit outside the range and off the gateway. Either would let another peer present the address the engine believes X-Forwarded-Proto from.
"""

import ipaddress
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import proxy_tls

_DEFAULT_SUBNET = "10.207.0.0/24"
_HOST = "dfe.example.com"
_VALID = [
    (_DEFAULT_SUBNET, "10.207.0.254", "10.207.0.0/25"),
    ("172.16.1.0/24", "172.16.1.254", "172.16.1.0/25"),
    ("10.20.0.0/16", "10.20.255.254", "10.20.0.0/17"),
    ("192.168.64.0/23", "192.168.65.254", "192.168.64.0/24"),
]


def _extension(*, certificate: Path, name: str) -> str:
    """Return one extension of a certificate as openssl prints it."""
    return _openssl(
        args=["x509", "-noout", "-ext", name, "-in", str(certificate)]
    ).stdout


def _network(*, name: str, project: str = "", pools: list[tuple[str, str]]) -> dict:
    """Return one network as `docker network inspect` describes it."""
    return {
        "IPAM": {
            "Config": [{"IPRange": rng, "Subnet": subnet} for subnet, rng in pools]
        },
        "Labels": {"com.docker.compose.project": project} if project else {},
        "Name": name,
    }


def _no_openssl(name: str) -> None:
    """Stand in for shutil.which on a host without openssl."""
    return None


def _openssl(*, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run openssl with args and capture what it prints."""
    return subprocess.run(
        ["openssl", *args],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )


@pytest.fixture
def minted(tmp_path: Path) -> Path:
    """A directory `mint` has written a set into for _HOST."""
    directory = tmp_path / "certs"
    proxy_tls.mint(cert_dir=str(directory), host=_HOST)
    return directory


@pytest.fixture
def pair(tmp_path: Path) -> Path:
    """A directory holding a real throwaway console.crt and a 0600 console.key."""
    directory = tmp_path / "certs"
    proxy_tls.throwaway_pair(directory=directory)
    return directory


@pytest.mark.parametrize(("subnet", "address", "allocation"), _VALID)
def test_the_proxy_takes_the_last_host_and_docker_the_first_half(
    subnet: str, address: str, allocation: str
) -> None:
    assert proxy_tls.derive(subnet=subnet) == (address, allocation)


@pytest.mark.parametrize(("subnet", "address", "allocation"), _VALID)
def test_no_container_and_no_gateway_can_hold_the_proxy_address(
    subnet: str, address: str, allocation: str
) -> None:
    network = ipaddress.IPv4Network(subnet)
    derived_address, derived_allocation = proxy_tls.derive(subnet=subnet)
    proxy = ipaddress.IPv4Address(derived_address)

    assert proxy in network
    assert proxy not in ipaddress.IPv4Network(derived_allocation)
    assert proxy != next(network.hosts())
    assert proxy != network.broadcast_address


@pytest.mark.parametrize(
    "subnet",
    [
        "0.0.0.0/0",
        "8.8.8.0/24",
        "100.64.0.0/24",
        "10.0.0.0/8",
        "10.0.0.0/15",
        "172.16.1.0/25",
        "172.16.1.5/24",
        "172.16.1.0",
        "bogus",
        "fd00::/64",
        "",
    ],
)
def test_a_subnet_that_is_public_or_the_wrong_size_is_refused(subnet: str) -> None:
    with pytest.raises(ValueError):
        proxy_tls.derive(subnet=subnet)


@pytest.mark.parametrize(
    ("tls", "pools", "stale"),
    [
        (True, [("172.16.1.0/24", "")], True),
        (True, [("10.99.0.0/24", "10.99.0.0/25")], True),
        (True, [(_DEFAULT_SUBNET, "10.207.0.0/25")], False),
        (False, [(_DEFAULT_SUBNET, "10.207.0.0/25")], True),
        (False, [("172.16.1.0/24", "")], False),
    ],
)
def test_a_reused_network_must_already_have_the_plan_the_dial_wants(
    pools: list[tuple[str, str]], stale: bool, tls: bool
) -> None:
    problem = proxy_tls.network_problem(
        networks=[_network(name="dfe-docker_default", pools=pools)],
        overlap=False,
        project="dfe-docker",
        subnet=_DEFAULT_SUBNET,
        tls=tls,
        toggle=True,
    )

    assert ("Run `make down` first" in problem) == stale


def test_a_network_the_stack_does_not_reuse_is_never_judged_stale() -> None:
    problem = proxy_tls.network_problem(
        networks=[_network(name="dfe-docker_default", pools=[("172.16.1.0/24", "")])],
        overlap=True,
        project="dfe-docker",
        subnet=_DEFAULT_SUBNET,
        tls=True,
        toggle=False,
    )

    assert problem == ""


@pytest.mark.parametrize(
    ("project", "own"),
    [
        ("dfe-docker", True),
        ("-DFE-Docker", True),
        ("dfe.docker", False),
        ("other", False),
    ],
)
def test_the_subnet_may_not_overlap_any_network_but_the_stacks_own(
    own: bool, project: str
) -> None:
    problem = proxy_tls.network_problem(
        networks=[_network(name="dfe-docker_default", pools=[("10.207.0.0/16", "")])],
        overlap=True,
        project=project,
        subnet=_DEFAULT_SUBNET,
        tls=True,
        toggle=False,
    )

    assert ("DFE_NETWORK_SUBNET='10.207.0.0/24' overlaps" in problem) != own


def test_an_unusable_subnet_is_named_before_any_network_is_read() -> None:
    problem = proxy_tls.network_problem(
        networks=[],
        overlap=True,
        project="dfe-docker",
        subnet="8.8.8.0/24",
        tls=True,
        toggle=False,
    )

    assert problem.startswith(
        "DFE_NETWORK_SUBNET='8.8.8.0/24' cannot hold the TLS network"
    )


def test_a_valid_pair_passes(pair: Path) -> None:
    assert proxy_tls.cert_problem(cert_dir=str(pair)) == ""


@pytest.mark.parametrize("name", ["console.crt", "console.key"])
def test_a_symlinked_file_is_refused_with_the_copy_to_make(
    name: str, pair: Path
) -> None:
    real = pair.parent / f"real-{name}"
    (pair / name).rename(real)
    (pair / name).symlink_to(real)

    problem = proxy_tls.cert_problem(cert_dir=str(pair))

    assert "is a symlink" in problem
    assert "cp -L" in problem


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("console.crt", "is not a PEM certificate"),
        ("console.key", "is not an unencrypted PEM private key"),
    ],
)
def test_an_empty_file_is_refused(name: str, pair: Path, said: str) -> None:
    (pair / name).write_text("", encoding="utf-8", newline="\n")

    assert said in proxy_tls.cert_problem(cert_dir=str(pair))


def test_a_key_from_another_pair_is_refused(pair: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    proxy_tls.throwaway_pair(directory=other)
    shutil.copy(dst=pair / "console.key", src=other / "console.key")

    assert "does not belong to" in proxy_tls.cert_problem(cert_dir=str(pair))


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads a 0000 file, so nothing is unreadable"
)
@pytest.mark.parametrize("name", ["console.crt", "console.key"])
def test_an_unreadable_file_is_named_as_unreadable_not_missing(
    name: str, pair: Path
) -> None:
    (pair / name).chmod(0o000)

    problem = proxy_tls.cert_problem(cert_dir=str(pair))

    assert "cannot read it" in problem
    assert "is missing" not in problem


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root enters a 0000 directory, so nothing is closed"
)
def test_a_cert_dir_this_user_cannot_open_is_refused_not_raised(pair: Path) -> None:
    pair.chmod(0o000)
    try:
        problem = proxy_tls.cert_problem(cert_dir=str(pair))
    finally:
        pair.chmod(0o700)

    assert "exists but this user cannot open it" in problem


@pytest.mark.parametrize(
    ("cert_dir", "said"),
    [
        ("certs", "must be a path starting with"),
        ("./no-such-dir", "is not a directory"),
    ],
)
def test_a_cert_dir_that_is_no_directory_is_refused(cert_dir: str, said: str) -> None:
    assert said in proxy_tls.cert_problem(cert_dir=cert_dir)


def test_a_cert_dir_pointing_at_a_file_is_refused(pair: Path) -> None:
    assert "is not a directory" in proxy_tls.cert_problem(
        cert_dir=str(pair / "console.crt")
    )


def test_a_host_without_openssl_is_told_so(
    monkeypatch: pytest.MonkeyPatch, pair: Path
) -> None:
    monkeypatch.setattr(proxy_tls.shutil, "which", _no_openssl)

    assert "openssl is not on PATH" in proxy_tls.cert_problem(cert_dir=str(pair))


def test_a_minted_set_is_three_files_the_precheck_accepts(minted: Path) -> None:
    assert sorted(path.name for path in minted.iterdir()) == [
        "ca.crt",
        "console.crt",
        "console.key",
    ]
    assert proxy_tls.cert_problem(cert_dir=str(minted)) == ""
    assert stat.S_IMODE((minted / "console.key").stat().st_mode) == 0o600


def test_the_minted_certificate_verifies_strictly_against_its_ca(
    minted: Path,
) -> None:
    result = _openssl(
        args=[
            "verify",
            "-x509_strict",
            "-CAfile",
            str(minted / "ca.crt"),
            str(minted / "console.crt"),
        ]
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith(": OK\n")


def test_the_ca_signs_no_other_ca_and_the_certificate_only_serves(
    minted: Path,
) -> None:
    assert "CA:TRUE, pathlen:0" in _extension(
        certificate=minted / "ca.crt", name="basicConstraints"
    )
    assert "TLS Web Server Authentication" in _extension(
        certificate=minted / "console.crt", name="extendedKeyUsage"
    )


@pytest.mark.parametrize(
    ("host", "names"),
    [
        (_HOST, "DNS:dfe.example.com, DNS:localhost, IP Address:127.0.0.1"),
        ("LOCALHOST", "DNS:localhost, IP Address:127.0.0.1"),
        ("127.0.0.1", "IP Address:127.0.0.1, DNS:localhost"),
        ("192.0.2.10", "IP Address:192.0.2.10, DNS:localhost, IP Address:127.0.0.1"),
        ("[::1]", "IP Address:0:0:0:0:0:0:0:1, DNS:localhost, IP Address:127.0.0.1"),
    ],
)
def test_the_certificate_names_the_host_then_localhost_and_loopback_once(
    tmp_path: Path, host: str, names: str
) -> None:
    proxy_tls.mint(cert_dir=str(tmp_path), host=host)

    assert names in _extension(
        certificate=tmp_path / "console.crt", name="subjectAltName"
    )


@pytest.mark.parametrize("name", ["ca.crt", "console.crt", "console.key"])
def test_an_existing_file_is_never_replaced(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("keep\n", encoding="utf-8", newline="\n")

    with pytest.raises(proxy_tls.MintError, match=f"already holds {name}"):
        proxy_tls.mint(cert_dir=str(tmp_path), host=_HOST)

    assert [path.name for path in tmp_path.iterdir()] == [name]
    assert (tmp_path / name).read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "a b",
        "evil.example,DNS:*.example.com",
        "*.example.com",
        "x/y",
        "a\nb",
        "-lead.example",
        f"{'a' * 64}.example",
        "fe80::1%eth0",
        "::1%x\nsubjectAltName=DNS:*.example.com",
    ],
)
def test_a_host_a_certificate_cannot_carry_is_refused(
    tmp_path: Path, host: str
) -> None:
    directory = tmp_path / "certs"

    with pytest.raises(proxy_tls.MintError, match="not a hostname or an IP address"):
        proxy_tls.mint(cert_dir=str(directory), host=host)

    assert not (directory.exists())


@pytest.mark.parametrize(
    ("host", "common_name"),
    [
        (f"{'a' * 33}.example", f"{'a' * 33}.example"),
        (f"{'a' * 56}.example", f"{'a' * 56}.example"),
        (f"{'a' * 57}.example", "DFE console"),
        (f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 53}.example", "DFE console"),
    ],
)
def test_a_long_host_mints_and_the_san_carries_it_whole(
    tmp_path: Path, host: str, common_name: str
) -> None:
    proxy_tls.mint(cert_dir=str(tmp_path), host=host)

    certificate = tmp_path / "console.crt"
    assert f"DNS:{host}" in _extension(certificate=certificate, name="subjectAltName")
    assert (
        _openssl(args=["x509", "-noout", "-subject", "-in", str(certificate)]).stdout
        == f"subject=CN={common_name}\n"
    )
    assert (
        _openssl(
            args=[
                "verify",
                "-x509_strict",
                "-CAfile",
                str(tmp_path / "ca.crt"),
                str(certificate),
            ]
        ).returncode
        == 0
    )


def test_a_failed_mint_leaves_no_key_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run_until_signing(*, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[1] == "x509":
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stderr="config noise\nthe real cause\n",
                stdout="",
            )
        return real_run(args=args)

    real_run = proxy_tls._run
    directory = tmp_path / "certs"
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(proxy_tls, "_run", run_until_signing)
    monkeypatch.setattr(proxy_tls.tempfile, "tempdir", str(system_temp))

    with pytest.raises(proxy_tls.MintError, match="the real cause$"):
        proxy_tls.mint(cert_dir=str(directory), host=_HOST)

    assert list(directory.iterdir()) == []
    assert list(system_temp.iterdir()) == []


def test_a_bare_cert_dir_is_refused() -> None:
    with pytest.raises(proxy_tls.MintError, match="must be a path starting with"):
        proxy_tls.mint(cert_dir="certs", host=_HOST)


def test_a_cert_dir_that_is_a_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "certs"
    target.write_text("", encoding="utf-8", newline="\n")

    with pytest.raises(proxy_tls.MintError, match="is a file, not a directory"):
        proxy_tls.mint(cert_dir=str(target), host=_HOST)


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root writes into a 0500 directory, so nothing is shut"
)
def test_a_cert_dir_this_user_cannot_write_is_refused_not_raised(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(proxy_tls.MintError, match="cannot write to"):
            proxy_tls.mint(cert_dir=str(tmp_path), host=_HOST)
    finally:
        tmp_path.chmod(0o700)


def test_a_host_without_openssl_mints_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(proxy_tls.shutil, "which", _no_openssl)
    directory = tmp_path / "certs"

    with pytest.raises(proxy_tls.MintError, match="openssl is not on PATH"):
        proxy_tls.mint(cert_dir=str(directory), host=_HOST)

    assert not (directory.exists())


def test_the_mint_command_says_what_to_trust(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["proxy_tls.py", "mint", "--cert-dir", str(tmp_path), "--host", _HOST],
    )

    assert proxy_tls.main() == 0
    captured = capsys.readouterr()
    assert f"Trust {tmp_path}/ca.crt once" in captured.out
    assert f"DFE_PROXY_CA_BUNDLE={tmp_path}/ca.crt" in captured.out
    assert captured.err == ""


def test_the_mint_command_refuses_on_stderr(
    capsys: pytest.CaptureFixture[str],
    minted: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["proxy_tls.py", "mint", "--cert-dir", str(minted), "--host", _HOST],
    )

    assert proxy_tls.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already holds" in captured.err


def test_the_network_command_prints_the_address_then_the_range(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["proxy_tls.py", "network", _DEFAULT_SUBNET])

    assert proxy_tls.main() == 0
    assert capsys.readouterr().out == "10.207.0.254 10.207.0.0/25\n"


def test_the_network_command_names_the_subnet_it_refuses_on_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["proxy_tls.py", "network", "172.16.1.0/26"])

    assert proxy_tls.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "DFE_NETWORK_SUBNET='172.16.1.0/26'" in captured.err
