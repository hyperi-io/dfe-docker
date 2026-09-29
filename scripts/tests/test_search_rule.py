#  Project:      dfe-docker
#  File:         tests/test_search_rule.py
#  Purpose:      Assert what the search-rule-hunt e2e test types, reads and requires
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The half of the search-rule-hunt e2e test that needs no browser and no stack.

What the search bar gets, how the rule id is read off the page Create Rule opens,
the hunt name the console's form accepts, and the fragments a stored rule has to
carry. The browser steps themselves run only against a stack.
"""

import re

import pytest

import _search_rule
from _common import REPO_ROOT

_E2E_TESTS = REPO_ROOT / "tests" / "e2e" / "e2e-tests.yaml"
# The console's hunt form refuses any other identifier.
_HUNT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def test_the_search_is_held_to_the_run_by_its_marker() -> None:
    condition = _search_rule.search_condition(
        marker="run-'1", where="toString(_json.event_type) = 'login_failure'"
    )

    assert condition == (
        "toString(`_tags`.marker) = 'run-\\'1' AND "
        "(toString(_json.event_type) = 'login_failure')"
    )


@pytest.mark.parametrize(
    ("url", "rule_id"),
    [
        ("http://localhost:3000/rules/hyperdx-rule", "hyperdx-rule"),
        ("http://localhost:3000/rules?name=hyperdx-rule-2", "hyperdx-rule-2"),
        ("http://localhost:3000/rules/noisy%20logins/", "noisy logins"),
    ],
)
def test_the_rule_is_read_off_either_url_the_console_shows_it_on(
    url: str, rule_id: str
) -> None:
    assert _search_rule.rule_id_from_url(url) == rule_id


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000/rules",
        "http://localhost:3000/rules?name=",
        "http://localhost:3000/observe/search",
        "http://localhost:3000/login?callbackUrl=%2Frules%2Fx",
    ],
)
def test_any_other_page_names_no_rule(url: str) -> None:
    assert _search_rule.rule_id_from_url(url) is None


@pytest.mark.parametrize(
    "marker",
    [
        "e2e-20260929010203-search-rule-hunt",
        "2026-run",
        "Run--With..Dots",
    ],
)
def test_the_hunt_name_is_one_the_console_form_accepts(marker: str) -> None:
    assert _HUNT_NAME.match(_search_rule.hunt_identifier(marker))


def test_the_hunt_name_keeps_the_marker_readable() -> None:
    assert (
        _search_rule.hunt_identifier("e2e-20260929010203-search-rule-hunt")
        == "e2e_20260929010203_search_rule_hunt"
    )


def test_a_stored_where_names_every_fragment_it_dropped() -> None:
    where = (
        "(toString(`_tags`.marker) = 'run-1' AND "
        "(toString(_json.event_type) = 'login_failure'))"
    )

    assert _search_rule.missing_fragments(
        where, ["run-1", "login_failure", "user_name", "'root'"]
    ) == ["user_name", "'root'"]
    assert _search_rule.missing_fragments(where, ["run-1"]) == []


def test_the_suite_defines_a_search_rule_test_with_both_filters() -> None:
    text = _E2E_TESTS.read_text(encoding="utf-8")
    block = text.split("- name: search-rule-hunt", 1)[1].split("- name:", 1)[0]

    assert "from_search:" in block
    assert "search: " in block
    assert "path: " in block
    assert "value: " in block
    assert "where:" not in block
