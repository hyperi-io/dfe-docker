#  Project:      dfe-docker
#  File:         tests/test_init.py
#  Purpose:      Assert `make init` asks the retention question once, and only where it can
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The retention question `make init` asks.

A TTY is asked once for a NEW .env and the answer lands live; a pipe keeps the
template's commented default; the environment pre-answers; junk is re-asked at
the prompt and refused from the environment; an existing .env is never re-asked.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import init

KEY = init.RETENTION_KEY
_TEMPLATE = f"""\
## A template with the retention placeholder.
# CLICKHOUSE_SECURE=false
#
## Days every time-series table keeps rows.
# {KEY}=90
"""
_SETTLED = "\n".join(f"{key}=already-minted-value" for key in init.GENERATED_SECRETS)


def _answers(*values: str) -> Iterator[str]:
    return iter(values)


@pytest.fixture
def layout(dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path init.main reads at the throwaway tree; return the template."""
    template = dotenv.parent / ".env.example"
    template.write_text(_TEMPLATE, encoding="utf-8", newline="\n")
    env_templates = dotenv.parent / "env.example"
    env_templates.mkdir()
    (env_templates / "engine.env").write_text("# DFE_API_PORT=8003\n", encoding="utf-8")
    monkeypatch.setattr(init, "DOTENV_TEMPLATE", template)
    monkeypatch.setattr(init, "ENV_TEMPLATE_DIR", env_templates)
    monkeypatch.setattr(init, "ENV_DIR", dotenv.parent / "env")
    monkeypatch.delenv(KEY, raising=False)
    return template


def _never_asked(_prompt: str = "") -> str:
    raise AssertionError("input() must not be called")


def test_a_tty_answer_is_written_live(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "30")

    assert init.main() == 0
    written = dotenv.read_text(encoding="utf-8")
    assert f"\n{KEY}=30\n" in written
    assert f"# {KEY}=90" not in written
    assert "# CLICKHOUSE_SECURE=false" in written


def test_junk_is_re_asked_until_a_whole_number_arrives(
    dotenv: Path,
    layout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = _answers("ninety", "-1", "1.5", "0")
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert init.main() == 0
    assert f"\n{KEY}=0\n" in dotenv.read_text(encoding="utf-8")
    assert capsys.readouterr().err.count("whole number of days") == 3


def test_an_empty_answer_takes_the_default(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert init.main() == 0
    assert f"\n{KEY}=90\n" in dotenv.read_text(encoding="utf-8")


def test_a_pipe_keeps_the_template_line(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", _never_asked)

    assert init.main() == 0
    written = dotenv.read_text(encoding="utf-8")
    assert f"# {KEY}=90" in written
    assert f"\n{KEY}=" not in written


def test_the_environment_pre_answers_without_a_prompt(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY, "14")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _never_asked)

    assert init.main() == 0
    assert f"\n{KEY}=14\n" in dotenv.read_text(encoding="utf-8")


def test_junk_in_the_environment_is_refused(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY, "soon")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit, match=KEY):
        init.main()


def test_a_template_without_the_key_still_gets_the_line(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout.write_text("# CLICKHOUSE_SECURE=false\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv(KEY, "7")

    assert init.main() == 0
    assert dotenv.read_text(encoding="utf-8").endswith(f"{KEY}=7\n")


def test_a_rerun_never_asks_and_leaves_the_file_alone(
    dotenv: Path, layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv.write_text(_SETTLED + "\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _never_asked)

    assert init.main() == 0
    assert dotenv.read_text(encoding="utf-8") == _SETTLED + "\n"
