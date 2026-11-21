#  Project:      dfe-docker
#  File:         tests/test_gnmic.py
#  Purpose:      Assert gnmic runs only beside the collector it pushes to, on a pinned image
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The gnmic footprint key, its compose service and its shipped config."""

import re

import pytest

import resolve_profile
from _common import COMPOSE_FILE, CONFIG_DIR, REPO_ROOT

_FOOTPRINT_VARS = (
    resolve_profile.GNMIC_ENABLED_ENV_VAR,
    resolve_profile.OTEL_ENABLED_ENV_VAR,
)


def _resolve(*, tmp_path, monkeypatch, environ: dict[str, str]) -> tuple[int, dict]:
    """Run the resolver and read back what it wrote."""
    written = tmp_path / ".tmp" / "profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    for key in _FOOTPRINT_VARS:
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    code = resolve_profile.main()
    values: dict[str, str] = {}
    if written.is_file():
        for line in written.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":=")
            values[name.replace("export", "").strip()] = value.strip()
    return code, values


@pytest.mark.parametrize(
    ("environ", "runs"),
    [
        pytest.param(
            {"DFE_GNMIC_ENABLED": "true", "DFE_OTEL_ENABLED": "true"},
            True,
            id="enabled-beside-the-collector",
        ),
        pytest.param({"DFE_OTEL_ENABLED": "true"}, False, id="off-by-default"),
        pytest.param(
            {"DFE_GNMIC_ENABLED": "false", "DFE_OTEL_ENABLED": "true"},
            False,
            id="switched-off",
        ),
    ],
)
def test_gnmic_runs_only_when_asked_for(
    tmp_path, monkeypatch, dotenv, environ: dict[str, str], runs: bool
) -> None:
    code, values = _resolve(tmp_path=tmp_path, monkeypatch=monkeypatch, environ=environ)

    assert code == 0
    assert ("--profile gnmic" in values["PROFILE_FLAGS"]) is runs
    assert ("gnmic" in values["DFE_SERVICES"].split()) is runs


def test_gnmic_without_the_collector_is_refused(tmp_path, monkeypatch, dotenv) -> None:
    code, _ = _resolve(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        environ={"DFE_GNMIC_ENABLED": "true", "DFE_OTEL_ENABLED": "false"},
    )

    assert code != 0


def test_the_gnmic_service_is_pinned_by_digest_and_opt_in() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    block = re.search(r"^  gnmic:\n(?P<body>(?:    .*\n|\n)+)", compose, re.MULTILINE)

    assert block, "docker-compose.yml has no gnmic service"
    body = block.group("body")
    assert re.search(r"image: \S+@sha256:[0-9a-f]{64}\}?$", body, re.MULTILINE)
    assert 'profiles: ["gnmic"]' in body


@pytest.mark.parametrize(
    "path",
    [
        CONFIG_DIR / "gnmic" / "gnmic.yaml",
        REPO_ROOT / "tests" / "gnmi" / "gnmic-emulator.yaml",
    ],
    ids=["shipped", "emulator"],
)
def test_each_gnmic_config_pushes_otlp_to_the_bundled_collector(path) -> None:
    text = path.read_text(encoding="utf-8")

    assert "type: otlp" in text
    assert "endpoint: otel-collector:4317" in text
    assert "service.name: gnmic" in text
