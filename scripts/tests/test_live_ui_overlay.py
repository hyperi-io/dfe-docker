#  Project:      dfe-docker
#  File:         scripts/tests/test_live_ui_overlay.py
#  Purpose:      Assert what the live-ui overlay's start script does to the checkout before next dev runs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The live-ui overlay's start script, run in a fake checkout with stand-in corepack and node."""

import os
import stat
import subprocess
from pathlib import Path

import yaml


def __plain_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> None:
    """Read a compose tag such as !override or !reset as no value."""
    return None


class _Tags(yaml.SafeLoader):
    """Reads compose's !override and !reset tags as plain values."""


_Tags.add_multi_constructor("!", __plain_tag)

REPO_ROOT = Path(__file__).resolve().parents[2]

_APP = Path("apps") / "dfe-core-ui"
# Notes whether Turbopack's dev cache is still there as `yarn workspace dfe-core-ui dev` starts
_COREPACK = """\
#!/bin/sh
if [ "$4" = "dev" ]; then
  if [ -e apps/dfe-core-ui/.next/dev ]; then echo kept; else echo cleared; fi > started
fi
"""
_NODE = "#!/bin/sh\nexit 0\n"
_OVERLAY = REPO_ROOT / "compose" / "live-ui.yml"


def _start_script() -> str:
    """The script the overlay hands `sh -c`."""
    overlay = yaml.load(_OVERLAY.read_text(encoding="utf-8"), Loader=_Tags)
    return overlay["services"]["dfe-ui"]["command"][2]


def _stand_in(*, directory: Path, name: str, text: str) -> None:
    """Write one executable stand-in for a binary the start script calls."""
    path = directory / name
    path.write_text(encoding="utf-8", newline="\n", data=text)
    path.chmod(mode=path.stat().st_mode | stat.S_IXUSR)


def test_each_start_clears_the_turbopack_dev_cache_before_next_dev(
    tmp_path: Path,
) -> None:
    cache = tmp_path / _APP / ".next" / "dev"
    cache.mkdir(parents=True)
    (cache / "stale").write_text(encoding="utf-8", data="built from older source")
    build = tmp_path / _APP / ".next" / "server"
    build.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stand_in(directory=bin_dir, name="corepack", text=_COREPACK)
    _stand_in(directory=bin_dir, name="node", text=_NODE)

    result = subprocess.run(
        args=["sh", "-c", _start_script()],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        encoding="utf-8",
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "started").read_text(encoding="utf-8").strip() == "cleared"
    # A host `next build` writes beside the dev cache and is left alone
    assert build.is_dir()
