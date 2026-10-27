#  Project:      dfe-docker
#  File:         tests/test_log_line.py
#  Purpose:      Assert script output carries a UTC timestamp and a level in scalo's text shape
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`_print` and the e2e runner's logger both write `<timestamp> | <level> | <name> - <msg>`.

That is the line scalo's text console logger writes for a record with no fields, so
a log read next to a service's carries the same columns.
"""

import datetime
import logging
import re
import sys
import types

import pytest

import _common

_LINE = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z \| (?P<level>[A-Z]+) +\| (?P<rest>.*)"
)
_WHEN = datetime.datetime(2026, 10, 1, 10, 47, 34, 727003, datetime.UTC)


def _printed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, **kwargs: str
) -> re.Match[str]:
    monkeypatch.setattr(sys, "argv", ["scripts/post.py"])
    _common._print(**kwargs)
    match = _LINE.fullmatch(capsys.readouterr().err.rstrip("\n"))
    assert match, "line does not carry a UTC timestamp and a level"
    return match


def _record(*, level: int, msg: str) -> logging.LogRecord:
    """Return a record at `level` stamped at _WHEN."""
    record = logging.LogRecord(
        args=None,
        exc_info=None,
        level=level,
        lineno=1,
        msg=msg,
        name="e2e",
        pathname=__file__,
    )
    record.created = _WHEN.timestamp()
    return record


@pytest.fixture
def e2e_formatter(
    e2e_runner: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> logging.Formatter:
    """The e2e runner's formatter, run as scripts/test_e2e.py with its PASS level named."""
    monkeypatch.setattr(name="argv", target=sys, value=["scripts/test_e2e.py"])
    logging.addLevelName(level=e2e_runner.PASS_LEVEL, levelName="PASS")
    return e2e_runner.ColouredFormatter(fmt="%(message)s")


@pytest.fixture
def e2e_runner() -> types.ModuleType:
    """The e2e runner module, skipping the test where it cannot be imported."""
    return pytest.importorskip("test_e2e")


def test_the_line_is_scalos_text_shape_to_the_millisecond() -> None:
    got = _common.log_line(level="WARNING", msg="key rejected", name="app", now=_WHEN)

    assert got == "2026-10-01T10:47:34.727Z | WARNING  | app - key rejected"


def test_a_non_utc_time_is_not_rendered_in_its_own_zone() -> None:
    brisbane = datetime.timezone(datetime.timedelta(hours=10))

    got = _common.log_line(
        level="INFO", msg="m", name="app", now=_WHEN.astimezone(tz=brisbane)
    )

    assert got.startswith("2026-10-01T10:47:34.727Z | ")


def test_the_level_word_is_the_only_thing_coloured() -> None:
    got = _common.log_line(
        colour="\033[1;31m", level="FAIL", msg="m", name="app", now=_WHEN
    )

    assert got == "2026-10-01T10:47:34.727Z | \033[1;31mFAIL\033[0m     | app - m"


def test_a_message_with_no_header_names_the_script(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    got = _printed(capsys, monkeypatch, msg="Created")

    assert (got["level"], got["rest"]) == ("INFO", "post - Created")


def test_a_header_follows_the_script_name(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    got = _printed(capsys, monkeypatch, header=".env", msg="Created")

    assert got["rest"] == "post:.env - Created"


@pytest.mark.parametrize(
    ("msg", "level"),
    [
        ("PASS engine converged", "PASS"),
        ("FAIL seed: boom", "FAIL"),
        ("SKIP events for source 'x'", "SKIP"),
        ("WARNING: cores read nothing", "WARNING"),
        ("error: boom", "ERROR"),
        ("Skipped (already exists)", "INFO"),
        ("FAILED is not the word FAIL", "INFO"),
    ],
)
def test_the_word_a_message_leads_with_is_its_level(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    msg: str,
    level: str,
) -> None:
    assert _printed(capsys, monkeypatch, msg=msg)["level"] == level


def test_the_message_text_is_unchanged(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    got = _printed(capsys, monkeypatch, msg="FAIL seed: boom")

    assert got["rest"].endswith(" - FAIL seed: boom")


def test_the_e2e_logger_writes_the_same_shape(
    e2e_formatter: logging.Formatter,
    e2e_runner: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_a_terminal() -> bool:
        return False

    monkeypatch.setattr(name="isatty", target=sys.stderr, value=not_a_terminal)

    got = e2e_formatter.format(
        record=_record(level=e2e_runner.PASS_LEVEL, msg="[simple] 1000/1000 rows")
    )

    assert (
        got
        == "2026-10-01T10:47:34.727Z | PASS     | test_e2e - [simple] 1000/1000 rows"
    )


def test_the_e2e_level_word_is_coloured_on_a_terminal_only(
    e2e_formatter: logging.Formatter,
    e2e_runner: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def a_terminal() -> bool:
        return True

    monkeypatch.setattr(name="isatty", target=sys.stderr, value=a_terminal)

    got = e2e_formatter.format(record=_record(level=e2e_runner.PASS_LEVEL, msg="ok"))

    assert " | \033[1;32mPASS\033[0m     | " in got
