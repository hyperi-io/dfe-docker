#  Project:      dfe-docker
#  File:         _console_tls.py
#  Purpose:      The console URL the e2e suite drives, and the certificate it accepts there
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Where the e2e suite reaches the console, and which certificate it accepts there.

Internal support module - imported by the e2e suite, not executed directly. Stdlib only, so the unit tests import it without PyYAML.

With DFE_PROXY_TLS off the console is http://localhost:<DFE_UI_PORT>. With it on, dfe-proxy answers https only, so the suite goes to DFE_EXTERNAL_ORIGIN's host on DFE_UI_PORT and verifies the certificate there, by chain and by name: against the system trust store, or against the PEM bundle DFE_PROXY_CA_BUNDLE names when a private CA signed it. Verification is never switched off.

Chrome takes no CA bundle, so the browser is pinned to the public key of the leaf certificate this module has just verified.
"""

import base64
import hashlib
import shutil
import socket
import ssl
import subprocess
import time
import typing
from pathlib import Path
from urllib.parse import urlsplit

from _common import PROXY_TLS_KEY, _published_url

CA_BUNDLE_VAR = "DFE_PROXY_CA_BUNDLE"
UI_URL_VAR = "DFE_UI_URL"
# OpenSSL's X509_V_ERR_* codes for the failures that need advice other than a CA bundle.
_NOT_YET_VALID = 9
_EXPIRED = 10
_HOSTNAME_MISMATCH = 62
# dfe-proxy starts after the services the suite gates on, so a connection it refuses is retried; a certificate that fails is not.
HANDSHAKE_TIMEOUT_SECONDS = 120.0
_HANDSHAKE_INTERVAL_SECONDS = 2.0
_CONNECT_TIMEOUT_SECONDS = 10.0


class ConsoleTrustError(Exception):
    """The console's certificate cannot be verified; the message names what to change."""


def console_url(*, environ: typing.Mapping[str, str]) -> str:
    """Return the console URL the suite drives: DFE_UI_URL when set, else the one dfe-proxy publishes.

    Plain http is http://localhost:<DFE_UI_PORT>. Under console TLS it is https at DFE_EXTERNAL_ORIGIN's host, the name the certificate carries.
    """
    explicit = environ.get(UI_URL_VAR, "").strip().rstrip("/")
    if explicit:
        return explicit
    if environ.get(PROXY_TLS_KEY, "").strip() != "true":
        port = (environ.get("DFE_UI_PORT", "").strip()) or ("3000")
        return f"http://localhost:{port}"
    return _published_url(
        default_port="3000",
        host="localhost",
        port_key="DFE_UI_PORT",
        proxied=True,
        values=environ,
    )


def tls_context(
    *, environ: typing.Mapping[str, str], url: str
) -> ssl.SSLContext | None:
    """Return the context that verifies the console's certificate, or None for a plain http console.

    Raises:
        ConsoleTrustError: DFE_PROXY_CA_BUNDLE names something that is not a loadable PEM bundle.
    """
    if urlsplit(url).scheme != "https":
        return None
    bundle = environ.get(CA_BUNDLE_VAR, "").strip()
    if not (bundle):
        context = ssl.create_default_context()
    elif not (Path(bundle).is_file()):
        raise ConsoleTrustError(
            f"{CA_BUNDLE_VAR}={bundle!r} is not a file. Point it at the PEM bundle of the CA chain that signed the console certificate"
        )
    else:
        try:
            context = ssl.create_default_context(cafile=bundle)
        except OSError as error:
            raise ConsoleTrustError(
                f"{CA_BUNDLE_VAR}={bundle!r} is not a PEM certificate bundle this user can load: {error}"
            ) from error
    # Both proxies offer TLS 1.2 and 1.3 only.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def trust_problem(
    *, environ: typing.Mapping[str, str], error: ssl.SSLCertVerificationError, url: str
) -> str:
    """Return what to change for a console certificate that did not verify."""
    host = urlsplit(url).hostname
    detail = (error.verify_message) or (str(error))
    if error.verify_code == _HOSTNAME_MISMATCH:
        return f"{url} presents a certificate that does not name {host} ({detail}). Reissue console.crt with {host} in its subjectAltName, or set DFE_EXTERNAL_ORIGIN to a name it carries"
    if error.verify_code in (_NOT_YET_VALID, _EXPIRED):
        return f"{url} presents a certificate outside its validity period ({detail}). Replace console.crt in DFE_PROXY_CERT_DIR"
    bundle = environ.get(CA_BUNDLE_VAR, "").strip()
    if bundle:
        return f"{url} presents a certificate that does not verify against {CA_BUNDLE_VAR}={bundle!r} ({detail}). Point {CA_BUNDLE_VAR} at the PEM bundle of the CA chain that signed console.crt"
    return f"{url} presents a certificate the system trust store does not accept ({detail}). Set {CA_BUNDLE_VAR} to the PEM bundle of the CA chain that signed console.crt"


def verify_console(
    *,
    context: ssl.SSLContext,
    environ: typing.Mapping[str, str],
    timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
    url: str,
) -> bytes:
    """Complete a verified TLS handshake with the console and return the certificate it presented, DER-encoded.

    Raises:
        ConsoleTrustError: the certificate does not verify, or no handshake completed within `timeout`.
    """
    parts = urlsplit(url)
    host = (parts.hostname) or ("")
    port = (parts.port) or (443)
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(
                (host, port), timeout=_CONNECT_TIMEOUT_SECONDS
            ) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    certificate = tls.getpeercert(binary_form=True)
        except ssl.SSLCertVerificationError as error:
            raise ConsoleTrustError(
                trust_problem(environ=environ, error=error, url=url)
            ) from error
        except OSError as error:
            if time.monotonic() >= deadline:
                raise ConsoleTrustError(
                    f"{url} completed no TLS handshake within {timeout:.0f}s: {error}"
                ) from error
            time.sleep(_HANDSHAKE_INTERVAL_SECONDS)
            continue
        if not (certificate):
            raise ConsoleTrustError(f"{url} completed a handshake with no certificate")
        return certificate


def chrome_pin(*, certificate: bytes) -> str:
    """Return the base64 SHA-256 of a DER certificate's public key, the form Chrome's --ignore-certificate-errors-spki-list takes.

    Raises:
        ConsoleTrustError: openssl is not on PATH or cannot read the certificate.
    """
    if shutil.which("openssl") is None:
        raise ConsoleTrustError(
            "openssl is not on PATH, so the suite cannot pin the browser to the console's verified key. Install openssl"
        )
    result = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-noout", "-pubkey"],
        capture_output=True,
        check=False,
        input=certificate,
    )
    if result.returncode != 0:
        reason = result.stderr.decode("utf-8", errors="replace").strip()
        raise ConsoleTrustError(
            f"openssl could not read the console's certificate: {reason}"
        )
    pem = result.stdout.decode("ascii", errors="replace")
    body = "".join(line for line in pem.splitlines() if not (line.startswith("-----")))
    digest = hashlib.sha256(base64.b64decode(body)).digest()
    return base64.b64encode(digest).decode("ascii")
