#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         tests/test_check_compose.py
#  Purpose:      Prove the dev-path reader keeps every compose line it matches,
#                so a goal printing two of them is asserted twice
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for check_compose's `make -n dev` reader.

The rest of check_compose.py shells out to `docker compose` and `make`, which
needs a resolved stack. This half is a parser over the output, so it is testable
on a string -- and it is the half that decides how much the LOCAL-path guard
actually looks at.
"""

from __future__ import annotations

import check_compose

_FRAGMENTS = "-f docker-compose.yml -f docker-compose.override.yml"


def test_it_reads_the_files_off_each_subcommand():
    found = check_compose._dev_compose_files(
        output=f"docker compose {_FRAGMENTS} pull\ndocker compose {_FRAGMENTS} up -d\n"
    )

    assert sorted(found) == ["pull", "up"]
    assert found["up"] == [["docker-compose.yml", "docker-compose.override.yml"]]


def test_a_second_line_does_not_hide_the_first():
    """One `up -d` with a fragment missing used to pass if a later one had it."""
    found = check_compose._dev_compose_files(
        output=(
            "docker compose -f docker-compose.yml up -d dfe-engine\n"
            f"docker compose {_FRAGMENTS} up -d\n"
        )
    )

    assert found["up"] == [
        ["docker-compose.yml"],
        ["docker-compose.yml", "docker-compose.override.yml"],
    ]


def test_a_goal_that_prints_no_compose_line_reads_as_absent():
    assert check_compose._dev_compose_files(output="echo nothing to do\n") == {}
