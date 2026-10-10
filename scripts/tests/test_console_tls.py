#  Project:      dfe-docker
#  File:         tests/test_console_tls.py
#  Purpose:      Assert the e2e suite reaches the console where it is served and verifies its certificate
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The console URL and certificate trust the e2e suite uses, against a real TLS server.

Each certificate is minted by openssl at test time and served by a stdlib TLS listener on loopback, so every verdict comes from a real handshake.
"""

import base64
import contextlib
import hashlib
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import _console_tls

_HOST = "localhost"


def _openssl(*args: str, data: bytes | None = None) -> bytes:
    """Run openssl and return its stdout, failing the test on a non-zero exit."""
    result = subprocess.run(
        ["openssl", *args], capture_output=True, check=False, input=data
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _mint_ca(*, directory: Path, name: str) -> Path:
    """Write a throwaway P-384 CA into directory and return its certificate."""
    directory.mkdir(parents=True, exist_ok=True)
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-384",
        "-nodes",
        "-sha384",
        "-days",
        "1",
        "-subj",
        f"/CN={name}",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        str(directory / f"{name}.key"),
        "-out",
        str(directory / f"{name}.crt"),
    )
    return directory / f"{name}.crt"


def _mint_leaf(*, ca: Path, directory: Path, host: str) -> tuple[Path, Path]:
    """Write a P-384 server pair for host, signed by ca, and return (certificate, key)."""
    key = directory / f"{host}.key"
    request = directory / f"{host}.csr"
    certificate = directory / f"{host}.crt"
    _openssl(
        "req",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-384",
        "-nodes",
        "-sha384",
        "-subj",
        f"/CN={host}",
        "-addext",
        f"subjectAltName=DNS:{host}",
        "-keyout",
        str(key),
        "-out",
        str(request),
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(request),
        "-CA",
        str(ca),
        "-CAkey",
        str(ca.with_suffix(".key")),
        "-CAcreateserial",
        "-days",
        "1",
        "-sha384",
        "-copy_extensions",
        "copyall",
        "-out",
        str(certificate),
    )
    return certificate, key


@contextlib.contextmanager
def _serve(*, certificate: Path, key: Path) -> Iterator[int]:
    """Serve TLS handshakes on a loopback port with this pair, yielding the port."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
    listener = socket.create_server(("127.0.0.1", 0))

    def _accept() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            # A client that refuses the certificate ends the handshake with an alert.
            with contextlib.suppress(OSError):
                with context.wrap_socket(connection, server_side=True):
                    pass

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()
        thread.join(timeout=5)


@pytest.fixture
def ca(tmp_path: Path) -> Path:
    """The CA that signs the console certificate."""
    return _mint_ca(directory=tmp_path / "ca", name="console-ca")


@pytest.fixture
def console(ca: Path, tmp_path: Path) -> Iterator[str]:
    """A TLS console URL whose certificate the `ca` fixture signed for localhost."""
    certificate, key = _mint_leaf(ca=ca, directory=tmp_path / "ca", host=_HOST)
    with _serve(certificate=certificate, key=key) as port:
        yield f"https://{_HOST}:{port}"


def _refusal(*, environ: dict[str, str], url: str) -> str:
    """Return the message verify_console raises for this URL."""
    context = _console_tls.tls_context(environ=environ, url=url)
    assert context is not None
    with pytest.raises(_console_tls.ConsoleTrustError) as raised:
        _console_tls.verify_console(context=context, environ=environ, url=url)
    return str(raised.value)


@pytest.mark.parametrize(
    ("environ", "url"),
    [
        ({}, "http://localhost:3000"),
        ({"DFE_UI_PORT": "47300"}, "http://localhost:47300"),
        ({"DFE_UI_PORT": ""}, "http://localhost:3000"),
        (
            {
                "DFE_EXTERNAL_ORIGIN": "http://dfe.example.test",
                "DFE_PROXY_TLS": "false",
            },
            "http://localhost:3000",
        ),
        (
            {
                "DFE_EXTERNAL_ORIGIN": "https://dfe.example.test",
                "DFE_PROXY_TLS": "true",
            },
            "https://dfe.example.test:3000",
        ),
        (
            {
                "DFE_EXTERNAL_ORIGIN": "https://dfe.example.test",
                "DFE_PROXY_TLS": "true",
                "DFE_UI_PORT": "443",
            },
            "https://dfe.example.test:443",
        ),
        (
            {"DFE_PROXY_TLS": "true", "DFE_UI_URL": "https://console.example.test/"},
            "https://console.example.test",
        ),
    ],
)
def test_the_console_url_follows_the_dial(environ: dict[str, str], url: str) -> None:
    assert _console_tls.console_url(environ=environ) == url


def test_a_plain_http_console_needs_no_context() -> None:
    assert _console_tls.tls_context(environ={}, url="http://localhost:3000") is None


def test_with_no_bundle_the_system_store_verifies_chain_and_name() -> None:
    context = _console_tls.tls_context(environ={}, url="https://dfe.example.test:443")

    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_a_bundle_that_is_no_file_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(_console_tls.ConsoleTrustError) as raised:
        _console_tls.tls_context(
            environ={"DFE_PROXY_CA_BUNDLE": str(tmp_path / "missing.pem")},
            url="https://dfe.example.test:443",
        )

    assert "DFE_PROXY_CA_BUNDLE=" in str(raised.value)
    assert "is not a file" in str(raised.value)


def test_a_bundle_holding_no_certificate_names_the_variable(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("not a certificate\n", encoding="utf-8", newline="\n")

    with pytest.raises(_console_tls.ConsoleTrustError) as raised:
        _console_tls.tls_context(
            environ={"DFE_PROXY_CA_BUNDLE": str(bundle)},
            url="https://dfe.example.test:443",
        )

    assert f"DFE_PROXY_CA_BUNDLE={str(bundle)!r} is not a PEM" in str(raised.value)


def test_the_signing_ca_verifies_the_console(ca: Path, console: str) -> None:
    environ = {"DFE_PROXY_CA_BUNDLE": str(ca)}
    context = _console_tls.tls_context(environ=environ, url=console)
    assert context is not None

    certificate = _console_tls.verify_console(
        context=context, environ=environ, url=console
    )

    assert ssl.DER_cert_to_PEM_cert(certificate).startswith("-----BEGIN CERTIFICATE")


def test_a_private_ca_without_the_bundle_names_the_variable(console: str) -> None:
    problem = _refusal(environ={}, url=console)

    assert "the system trust store does not accept" in problem
    assert "Set DFE_PROXY_CA_BUNDLE" in problem


def test_another_cas_bundle_is_named_as_the_one_that_failed(
    console: str, tmp_path: Path
) -> None:
    other = _mint_ca(directory=tmp_path / "other", name="other-ca")

    problem = _refusal(environ={"DFE_PROXY_CA_BUNDLE": str(other)}, url=console)

    assert f"does not verify against DFE_PROXY_CA_BUNDLE={str(other)!r}" in problem


def test_a_certificate_for_another_name_is_refused_by_name(
    ca: Path, tmp_path: Path
) -> None:
    certificate, key = _mint_leaf(
        ca=ca, directory=tmp_path / "ca", host="other.example.test"
    )
    with _serve(certificate=certificate, key=key) as port:
        problem = _refusal(
            environ={"DFE_PROXY_CA_BUNDLE": str(ca)}, url=f"https://{_HOST}:{port}"
        )

    assert f"does not name {_HOST}" in problem
    assert "DFE_PROXY_CA_BUNDLE" not in problem


def test_a_console_that_never_answers_fails_within_the_timeout() -> None:
    with socket.create_server(("127.0.0.1", 0)) as reserved:
        port = reserved.getsockname()[1]
    url = f"https://127.0.0.1:{port}"
    context = _console_tls.tls_context(environ={}, url=url)
    assert context is not None

    with pytest.raises(_console_tls.ConsoleTrustError) as raised:
        _console_tls.verify_console(context=context, environ={}, timeout=0.1, url=url)

    assert "completed no TLS handshake within" in str(raised.value)


def test_the_browser_pin_is_the_leafs_public_key_digest(ca: Path, console: str) -> None:
    environ = {"DFE_PROXY_CA_BUNDLE": str(ca)}
    context = _console_tls.tls_context(environ=environ, url=console)
    assert context is not None
    certificate = _console_tls.verify_console(
        context=context, environ=environ, url=console
    )
    public_key = _openssl(
        "x509", "-inform", "DER", "-noout", "-pubkey", data=certificate
    )
    spki = _openssl("pkey", "-pubin", "-outform", "DER", data=public_key)

    expected = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")

    assert _console_tls.chrome_pin(certificate=certificate) == expected
