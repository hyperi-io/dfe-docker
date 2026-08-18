#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/check_docs.py
#  Purpose:      Assert every relative link in the docs set still resolves
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Assert that no document links at a file that is not there.

The docs are audience-scoped documents under `docs/` plus the README that routes
between them. They cross-link heavily and the README's whole job is to send
people elsewhere, so a deleted or renamed file is a silent breakage: the link
still looks like a link, and the reader finds out, not the author.

Scope, so a green run is not read as more than it earns:

- Relative links only. External URLs are not fetched -- that would make a hermetic
  check depend on the network and on someone else's uptime.
- The FILE half of a link is resolved, not the `#anchor`. A link to a real file
  with a stale anchor still passes here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from _common import REPO_ROOT, _print, _rel_path

# Markdown inline links: the `](target)` half. Reference-style links are not used
# anywhere in this docs set; if they appear, extend this rather than assume.
_LINK = re.compile(r"\]\(([^)]+)\)")

# Not fetched, for the reason in the module docstring.
_EXTERNAL = ("http://", "https://", "mailto:")


# Checked when present, absent without complaint. STATE.md is the real file behind
# the CLAUDE.md symlink: it carries the doc pointers an agent reads first, so it
# rots the same way, but both are gitignored to keep the tracked tree clean. It
# exists in a working checkout and never on CI -- requiring it would fail every CI
# run, and ignoring it locally would miss the file most likely to send a reader at
# something that has moved.
_OPTIONAL = ("STATE.md",)


# The README is the only prose left at the root, and it is required rather than
# globbed: a glob over docs/ alone would stop watching it the moment it moved.
_ROOT_DOCS = ("README.md",)


def _documents() -> list[Path]:
    """Return every document that ships prose, root files included."""
    docs = sorted((REPO_ROOT / "docs").glob("*.md"))
    required = [REPO_ROOT / name for name in _ROOT_DOCS]
    optional = [REPO_ROOT / name for name in _OPTIONAL]
    return [*required, *(p for p in optional if p.is_file()), *docs]


def _broken_links(path: Path) -> list[str]:
    """Return one message per relative link in `path` whose target is missing."""
    broken = []
    for target in _LINK.findall(path.read_text(encoding="utf-8")):
        if target.startswith(_EXTERNAL) or target.startswith("#"):
            continue
        # Split the anchor off: only the file half is resolvable here.
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        if not (resolved.exists()):
            broken.append(f"{_rel_path(path=path)} -> {target}")
    return broken


def main() -> int:
    documents = _documents()
    missing = [p for p in documents if not (p.is_file())]
    broken = [msg for p in documents if p.is_file() for msg in _broken_links(p)]

    for path in missing:
        _print(msg=f"FAIL {_rel_path(path=path)} is missing")
    for message in broken:
        _print(msg=f"FAIL {message}")
    if missing or broken:
        _print(msg=f"{len(missing)} missing file(s), {len(broken)} broken link(s)")
        return 1
    _print(msg=f"Every relative link resolves across {len(documents)} document(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
