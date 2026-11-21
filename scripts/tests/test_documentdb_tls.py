#  Project:      dfe-docker
#  File:         scripts/tests/test_documentdb_tls.py
#  Purpose:      Assert the DocumentDB TLS one-shot renews its pair without breaking the containers that mount it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""hyperdx-documentdb and dfe-hyperdx mount the one-shot's ca and server directories by subpath.

Removing a directory leaves a running container holding the deleted one, so dfe-hyperdx then
fails to open its CA file. The one-shot keeps the directories, leaves a valid pair alone and
renames each new file into place whole. These run the one-shot's own script against a temp
directory, with chown stubbed because the test user is not root.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ONE_SHOTS = Path(__file__).resolve().parents[2] / "compose" / "one-shots.yml"
FILES = ("ca/ca.crt", "server/tls.key", "server/tls.crt")
REWRITE = "wrote a new CA and gateway certificate"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


class _Tags(yaml.SafeLoader):
    """Reads compose's !override and !reset tags as plain values."""


_Tags.add_multi_constructor("!", lambda loader, suffix, node: None)


def _run(tls: Path, shims: Path) -> str:
    services = yaml.load(ONE_SHOTS.read_text(encoding="utf-8"), Loader=_Tags)[
        "services"
    ]
    script = services["hyperdx-documentdb-tls"]["command"][0].replace(
        "cd /tls", f"cd {tls}"
    )
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        check=True,
        encoding="utf-8",
        env={"PATH": f"{shims}:/usr/bin:/bin"},
        errors="replace",
        text=True,
    )
    return result.stdout


def _digests(tls: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((tls / name).read_bytes()).hexdigest() for name in FILES
    }


@pytest.fixture
def tls(tmp_path: Path) -> tuple[Path, Path]:
    shims = tmp_path / "shims"
    shims.mkdir()
    chown = shims / "chown"
    chown.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    chown.chmod(0o755)
    volume = tmp_path / "tls"
    volume.mkdir()
    return volume, shims


def test_a_valid_pair_is_left_alone(tls: tuple[Path, Path]) -> None:
    volume, shims = tls
    assert REWRITE in _run(volume, shims)
    before = _digests(volume)

    assert REWRITE not in _run(volume, shims)
    assert _digests(volume) == before


@pytest.mark.parametrize("broken", FILES, ids=["no-ca", "no-key", "no-leaf"])
def test_a_missing_file_renews_the_pair_in_the_same_directories(
    tls: tuple[Path, Path], broken: str
) -> None:
    volume, shims = tls
    _run(volume, shims)
    inodes = {name: (volume / name).stat().st_ino for name in ("ca", "server")}
    before = _digests(volume)
    (volume / broken).write_bytes(b"")

    said = _run(volume, shims)
    assert {name: (volume / name).stat().st_ino for name in ("ca", "server")} == inodes
    assert all(_digests(volume)[name] != before[name] for name in FILES)
    assert REWRITE in said
    assert not list(volume.rglob("*.new"))
    verify = subprocess.run(
        [
            "openssl",
            "verify",
            "-CAfile",
            str(volume / "ca/ca.crt"),
            str(volume / "server/tls.crt"),
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    assert verify.returncode == 0, verify.stderr
