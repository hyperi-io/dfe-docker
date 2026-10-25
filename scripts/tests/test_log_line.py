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


def test_the_line_is_scalos_text_shape_to_the_millisecond() -> None:
    got = _common.log_line(level="WARNING", name="app", msg="key rejected", now=_WHEN)

    assert got == "2026-10-01T10:47:34.727Z | WARNING  | app - key rejected"


def test_a_non_utc_time_is_not_rendered_in_its_own_zone() -> None:
    brisbane = datetime.timezone(datetime.timedelta(hours=10))

    got = _common.log_line(
        level="INFO", name="app", msg="m", now=_WHEN.astimezone(brisbane)
    )

    assert got.startswith("2026-10-01T10:47:34.727Z | ")


def test_the_level_word_is_the_only_thing_coloured() -> None:
    got = _common.log_line(
        level="FAIL", name="app", msg="m", colour="\033[1;31m", now=_WHEN
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


@pytest.fixture
def e2e_formatter(monkeypatch: pytest.MonkeyPatch):
    test_e2e = pytest.importorskip("test_e2e")
    monkeypatch.setattr(sys, "argv", ["scripts/test_e2e.py"])
    logging.addLevelName(test_e2e.PASS_LEVEL, "PASS")
    return test_e2e.ColouredFormatter("%(message)s")


def _record(level: int, msg: str) -> logging.LogRecord:
    record = logging.LogRecord("e2e", level, __file__, 1, msg, None, None)
    record.created = _WHEN.timestamp()
    return record


def test_the_e2e_logger_writes_the_same_shape(
    e2e_formatter: logging.Formatter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)

    got = e2e_formatter.format(_record(25, "[simple] 1000/1000 rows"))

    assert (
        got
        == "2026-10-01T10:47:34.727Z | PASS     | test_e2e - [simple] 1000/1000 rows"
    )


def test_the_e2e_level_word_is_coloured_on_a_terminal_only(
    e2e_formatter: logging.Formatter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    got = e2e_formatter.format(_record(25, "ok"))

    assert " | \033[1;32mPASS\033[0m     | " in got
