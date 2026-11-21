#  Project:      dfe-docker
#  File:         scripts/tests/test_documentdb_tls.py
#  Purpose:      Assert the DocumentDB TLS one-shot renews its pair without breaking the containers that mount it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""hyperdx-documentdb and dfe-hyperdx mount the one-shot's ca and server directories by subpath.

Removing a directory leaves a running container holding the deleted one, so dfe-hyperdx then
fails to open its CA file. The one-shot keeps the directories and replaces their files.
"""

from pathlib import Path

import pytest

ONE_SHOTS = Path(__file__).resolve().parents[2] / "compose" / "one-shots.yml"


def _script() -> str:
    text = ONE_SHOTS.read_text(encoding="utf-8")
    start = text.index("  hyperdx-documentdb-tls:")
    end = text.index("restart:", start)
    return text[start:end]


@pytest.mark.parametrize(
    ("fragment", "present"),
    [
        ("mkdir -p ca server", True),
        ("rm -rf", False),
        ("mkdir ca server", False),
        ("docker compose restart hyperdx-documentdb", True),
    ],
    ids=["keeps-dirs", "no-dir-removal", "no-bare-mkdir", "says-to-restart"],
)
def test_the_one_shot_keeps_its_mounted_directories(
    fragment: str, present: bool
) -> None:
    assert (fragment in _script()) is present
