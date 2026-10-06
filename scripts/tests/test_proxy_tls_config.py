#  Project:      dfe-docker
#  File:         tests/test_proxy_tls_config.py
#  Purpose:      Hold each console TLS proxy config to its plain config plus marked blocks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The *.tls.yaml proxy configs are their plain configs with marked TLS blocks added and nothing else.

The plain config is what every stack runs and the TLS one is what DFE_PROXY_TLS=true mounts instead, so an edit made to one alone would leave the two stacks routing differently without any check noticing. Stripping the blocks between the markers has to give back the plain file exactly.

dfe-hyperdx-proxy's TLS chains repeat the filters of the plain chain that follows each one, because YAML cannot point at a node that carries no anchor, so those copies are held equal too.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = REPO_ROOT / "config" / "proxy"

_BEGIN = "# BEGIN TLS"
_END = "# END TLS"
_PAIRS = [("envoy.yaml", "envoy.tls.yaml"), ("hyperdx.yaml", "hyperdx.tls.yaml")]
# Each config's listeners by name and port and whether they also answer plain HTTP on the same port.
_LISTENERS = [
    (
        "envoy.yaml",
        "envoy.tls.yaml",
        {"hyperdx-embed": "8091", "ingress": "8080"},
        False,
    ),
    (
        "hyperdx.yaml",
        "hyperdx.tls.yaml",
        {"hyperdx-api": "8000", "hyperdx-app": "8090"},
        True,
    ),
]
_LISTENER_NAME = re.compile(r"^    - name: (\S+)$")
# The key a filter chain's network filters sit under, with or without the list dash.
_FILTERS_KEYS = ("filters:", "- filters:")


def _filters_blocks(*, text: str) -> list[tuple[bool, list[str]]]:
    """Return every `filters:` block in file order, each with whether it sits inside a TLS block."""
    lines = text.splitlines()
    blocks = []
    inside = False
    for index, line in enumerate(lines):
        marker = line.strip()
        if marker == _BEGIN:
            inside = True
        elif marker == _END:
            inside = False
        if marker not in _FILTERS_KEYS:
            continue
        column = len(line) - len(line.lstrip(" -"))
        body = []
        for following in lines[index + 1 :]:
            indent = len(following) - len(following.lstrip(" "))
            if following.strip() and indent <= column:
                break
            body.append(following)
        blocks.append((inside, body))
    return blocks


def _listeners(*, text: str) -> dict[str, list[str]]:
    """Return each listener's lines by listener name, from `listeners:` to `clusters:`."""
    blocks = {}
    current = None
    inside = False
    for line in text.splitlines():
        if line == "  listeners:":
            inside = True
            continue
        if line == "  clusters:":
            break
        if not (inside):
            continue
        match = _LISTENER_NAME.match(line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks


def _strip_tls(*, text: str) -> str:
    """Return text with every block from a BEGIN TLS line to its END TLS line removed, both markers included."""
    kept = []
    inside = False
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        marker = line.strip()
        if marker == _BEGIN:
            if inside:
                raise ValueError(f"line {number}: {_BEGIN!r} inside an open block")
            inside = True
        elif marker == _END:
            if not (inside):
                raise ValueError(f"line {number}: {_END!r} with no block open")
            inside = False
        elif not (inside):
            kept.append(line)
    if inside:
        raise ValueError(f"{_BEGIN!r} is never closed")
    return "".join(kept)


@pytest.mark.parametrize(("plain", "tls"), _PAIRS)
def test_stripping_the_tls_blocks_gives_back_the_plain_config(
    plain: str, tls: str
) -> None:
    tls_text = (PROXY_DIR / tls).read_text(encoding="utf-8")

    assert _strip_tls(text=tls_text) == (PROXY_DIR / plain).read_text(encoding="utf-8")


@pytest.mark.parametrize(("plain", "tls", "listeners", "dual"), _LISTENERS)
def test_every_listener_terminates_tls_once_and_only_in_the_tls_config(
    dual: bool, listeners: dict[str, str], plain: str, tls: str
) -> None:
    encrypted = _listeners(text=(PROXY_DIR / tls).read_text(encoding="utf-8"))
    unencrypted = _listeners(text=(PROXY_DIR / plain).read_text(encoding="utf-8"))

    assert sorted(encrypted) == sorted(listeners)
    assert sorted(unencrypted) == sorted(listeners)
    for name, port in listeners.items():
        block = "\n".join(encrypted[name])
        assert f"port_value: {port}" in block
        assert block.count("transport_socket:") == 1, name
        assert ("transport_protocol: tls" in block) == dual, name
        assert ("tls_inspector" in block) == dual, name
        assert "transport_socket:" not in "\n".join(unencrypted[name])


def test_each_tls_chain_repeats_the_plain_filters_after_it() -> None:
    blocks = _filters_blocks(
        text=(PROXY_DIR / "hyperdx.tls.yaml").read_text(encoding="utf-8")
    )
    copies = [index for index, (inside, _) in enumerate(blocks) if inside]

    assert copies, "no TLS chain carries filters of its own, so nothing was compared"
    for index in copies:
        assert index + 1 < len(blocks)
        assert not (blocks[index + 1][0])
        assert blocks[index][1] == blocks[index + 1][1]


@pytest.mark.parametrize(
    "text",
    [
        f"{_BEGIN}\nkept: no\n",
        f"{_END}\n",
        f"{_BEGIN}\n{_BEGIN}\n{_END}\n",
    ],
)
def test_an_unbalanced_marker_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        _strip_tls(text=text)
