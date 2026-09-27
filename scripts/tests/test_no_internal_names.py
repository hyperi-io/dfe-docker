#  Project:      dfe-docker
#  File:         scripts/tests/test_no_internal_names.py
#  Purpose:      Refuse an internal estate name anywhere in the tracked tree
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Refuse an internal estate name anywhere a reader of the public repo can see it.

dfe-docker is a product other organisations deploy, and it goes public, so every
committed byte ships to the world. The development estate's name, the private
repo that runs it, its OpenBao path, a host nickname or a private address written
down here tells an outsider how that estate is laid out, and it cannot be taken
back once the visibility flips. Documentation names exist for this: RFC 2606
``example.com`` / ``example.test`` for hostnames, and the RFC 5737 ranges
192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24 for addresses.

dfe-infra carries the same guard, over the same pattern.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The estate's name (also its zone), the private repo that runs it, its OpenBao
# base path, host nicknames and the two private ranges it numbers, all matched
# case-insensitively. The OpenBao path stops short of a hyphen because
# `secret/dfe-<name>` is kubectl's reference to a product Secret.
INTERNAL = re.compile(
    r"devex"
    r"|hyperi-infra"
    r"|secret/dfe(?![\w-])"
    r"|tyrell"
    r"|hypersec"
    r"|10\.66\."
    r"|10\.1\.2\."
    r"|dragonfly"
    r"|desktop-derek"
    r"|ghostburner"
    r"|proxmox",
    re.IGNORECASE,
)

# This file carries the pattern itself.
EXCLUDED = {"scripts/tests/test_no_internal_names.py"}
EXCLUDED_PREFIXES: tuple[str, ...] = ()


def _tracked_files() -> list[str]:
    """Return every file git tracks -- exactly the set that ships when public."""
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    return [name for name in listed.split("\0") if name]


def _in_scope(name: str) -> bool:
    return name not in EXCLUDED and not name.startswith(EXCLUDED_PREFIXES)


def _hits(name: str) -> list[str]:
    try:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        # A binary blob carries no prose, and a listed-but-absent path is a gitlink.
        return []
    return [
        f"{name}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if INTERNAL.search(line)
    ]


def test_no_tracked_file_names_the_internal_estate() -> None:
    found: list[str] = []
    for name in _tracked_files():
        if _in_scope(name):
            found += _hits(name)
    assert not found, (
        "internal estate names found -- this repo goes public, so use a "
        "documentation name (example.com, example.test) or an RFC 5737 "
        "address instead:\n" + "\n".join(found)
    )


def test_the_sweep_would_catch_a_leak() -> None:
    """The guard is worth nothing if the pattern never matches, so prove it does."""
    for sample in (
        "k8s-1.devex.hyperi.io",
        "on the DevEx fleet",
        "hyperi-io/hyperi-infra",
        "ref: secret/dfe",
        "secret/dfe/ghcr-pull-secret",
        "(secret/dfe)",
        "http://10.66.252.19",
        "Proxmox VE",
        "dragonfly",
        "ghostburner",
    ):
        assert INTERNAL.search(sample), sample


def test_a_kubernetes_secret_reference_is_not_a_vault_path() -> None:
    """Kubectl names a product Secret `secret/dfe-<name>`; that is product text."""
    for sample in (
        "secret/dfe-cluster",
        "secret/dfe-fetcher-credentials configured",
    ):
        assert not INTERNAL.search(sample), sample
