#  Project:      dfe-docker
#  File:         tests/test_e2e_acks_off.py
#  Purpose:      Assert the acknowledgements-off outage twins run what their names say
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An `-acks-off` outage test runs acks-off configs and records its loss; no other does.

A kill test that proves no answered record is lost only means something with
acknowledgements held, and its twin only documents the opt-out when every hop it
overrides has them off. A text read, because these tests run with no PyYAML.
"""

import re
from pathlib import Path

from _common import REPO_ROOT

_E2E_TESTS = REPO_ROOT / "tests" / "e2e" / "e2e-tests.yaml"
_E2E_CONFIGS = REPO_ROOT / "tests" / "e2e" / "config"
_OVERRIDE = re.compile(r"^\s+(dfe-[\w-]+): (\S+\.yaml)$")


def _test_blocks() -> dict[str, list[str]]:
    """Return each e2e test's definition lines, by name."""
    blocks: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in _E2E_TESTS.read_text(encoding="utf-8").splitlines():
        if raw.startswith("  - name: "):
            current = blocks.setdefault(raw.split(": ", 1)[1].strip(), [])
        elif current is not None and raw.startswith("    "):
            current.append(raw)
    return blocks


def _overrides(lines: list[str]) -> dict[str, str]:
    """Return {service: config path} from one test's `config_overrides`."""
    return {
        match.group(1): match.group(2)
        for line in lines
        if (match := _OVERRIDE.match(line))
    }


def _acknowledgements(path: Path) -> list[str]:
    """Return the `enabled` value under each `acknowledgements:` block of a config."""
    values = []
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not (line.strip().startswith("#"))
    ]
    for index, line in enumerate(lines):
        if line == "acknowledgements:" and index + 1 < len(lines):
            values.append(lines[index + 1].removeprefix("enabled:").strip())
    return values


def test_the_config_reader_finds_each_acknowledgements_block(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "server:\n  acknowledgements:\n    # held\n    enabled: false\n"
        "grpc:\n  acknowledgements:\n    enabled: true\n",
        encoding="utf-8",
    )

    assert _acknowledgements(config) == ["false", "true"]


def test_every_config_override_exists() -> None:
    missing = {
        name: path
        for name, lines in _test_blocks().items()
        for path in _overrides(lines).values()
        if not ((REPO_ROOT / path).is_file())
    }

    assert missing == {}


def test_every_acks_off_config_turns_every_acknowledgement_off() -> None:
    configs = sorted(_E2E_CONFIGS.glob("*acks-off*.yaml"))

    held = {path.name: _acknowledgements(path) for path in configs}

    assert configs
    assert {name: values for name, values in held.items() if values != ["false"]} == {}


def test_no_other_e2e_config_turns_an_acknowledgement_off() -> None:
    others = [
        path for path in _E2E_CONFIGS.glob("*.yaml") if "acks-off" not in path.name
    ]

    off = {path.name for path in others if "false" in _acknowledgements(path)}

    assert off == set()


def test_every_acks_off_test_expects_loss_and_overrides_only_with_acks_off() -> None:
    twins = {
        name: lines
        for name, lines in _test_blocks().items()
        if name.endswith("-acks-off")
    }

    wrong = {
        name: sorted(_overrides(lines).values())
        for name, lines in twins.items()
        if "      expect_loss: true" not in lines
        or not (_overrides(lines))
        or any("acks-off" not in path for path in _overrides(lines).values())
    }

    assert twins
    assert wrong == {}


def test_only_an_acks_off_test_expects_loss() -> None:
    expecting = {
        name
        for name, lines in _test_blocks().items()
        if "      expect_loss: true" in lines
    }

    assert expecting
    assert {name for name in expecting if not (name.endswith("-acks-off"))} == set()
