#  Project:      dfe-docker
#  File:         tests/test_check_hardfail.py
#  Purpose:      Assert no compose image can resolve to an undigested reference
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Every `image:` in docker-compose.yml resolves to a digest.

The hard-fail run beside this proves an unpinned stack aborts, but compose stops at the FIRST missing variable, so it says nothing about the images behind it. These cases pin the shapes instead: which references the reader accepts, which it refuses, and that the committed file is clean.
"""

from __future__ import annotations

from pathlib import Path

import check_hardfail


def _compose(tmp_path: Path, monkeypatch, body: str) -> None:
    """Point the checker at a compose file carrying `body`."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(f"services:\n{body}", encoding="utf-8", newline="\n")
    monkeypatch.setattr(check_hardfail, "COMPOSE_FILE", path)


_DIGEST = "@sha256:" + "a" * 64


def test_the_committed_compose_file_has_no_undigested_image() -> None:
    assert check_hardfail._undigested_images() == []


def test_a_mandatory_pin_is_accepted(tmp_path: Path, monkeypatch) -> None:
    _compose(
        tmp_path,
        monkeypatch,
        "  ch:\n    image: clickhouse/clickhouse-server:${CLICKHOUSE_VERSION:?pin it}\n",
    )

    assert check_hardfail._undigested_images() == []


def test_a_default_carrying_a_digest_is_accepted(tmp_path: Path, monkeypatch) -> None:
    _compose(
        tmp_path,
        monkeypatch,
        f"  proxy:\n    image: quay.io/oauth2-proxy:${{PROXY_VERSION:-v7.15.4{_DIGEST}}}\n",
    )

    assert check_hardfail._undigested_images() == []


def test_a_default_tag_with_no_digest_is_refused(tmp_path: Path, monkeypatch) -> None:
    _compose(
        tmp_path,
        monkeypatch,
        "  ui:\n    image: ghcr.io/hyperi-io/dfe-ui:${DFE_UI_VERSION:-latest}\n",
    )

    findings = check_hardfail._undigested_images()

    assert len(findings) == 1
    assert "dfe-ui" in findings[0]


def test_a_literal_tag_is_refused(tmp_path: Path, monkeypatch) -> None:
    _compose(tmp_path, monkeypatch, "  kafka:\n    image: apache/kafka:4.1.0\n")

    assert len(check_hardfail._undigested_images()) == 1


def test_a_registry_default_does_not_stand_in_for_the_tag(
    tmp_path: Path, monkeypatch
) -> None:
    """The tag is the LAST interpolation -- a digest in the registry prefix is not one."""
    _compose(
        tmp_path,
        monkeypatch,
        f"  engine:\n    image: ${{IMAGE_REGISTRY:-ghcr.io/hyperi-io{_DIGEST}}}"
        "/dfe-engine:${DFE_ENGINE_VERSION:-latest}\n",
    )

    assert len(check_hardfail._undigested_images()) == 1


def test_the_finding_names_the_file_and_line(tmp_path: Path, monkeypatch) -> None:
    _compose(tmp_path, monkeypatch, "  kafka:\n    image: apache/kafka:4.1.0\n")

    assert check_hardfail._undigested_images()[0].startswith("docker-compose.yml:3:")
