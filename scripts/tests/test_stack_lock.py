#  Project:      dfe-docker
#  File:         scripts/tests/test_stack_lock.py
#  Purpose:      Assert the stack lock excludes a second holder and frees itself
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The lock `make proof` and the daemon updater share.

flock conflicts between separate open file descriptions even inside one process,
so a second `stack_lock` here stands in for the other program.
"""

import os
import stat
from pathlib import Path

import pytest

import _stack_lock


def test_a_second_holder_is_refused_while_the_first_has_it(tmp_path: Path) -> None:
    with _stack_lock.stack_lock(directory=tmp_path) as first:
        assert first
        with _stack_lock.stack_lock(directory=tmp_path) as second:
            assert not second


def test_the_lock_is_free_again_after_the_holder_leaves(tmp_path: Path) -> None:
    with _stack_lock.stack_lock(directory=tmp_path):
        pass
    with _stack_lock.stack_lock(directory=tmp_path) as again:
        assert again


def test_the_lock_is_released_when_the_holder_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), _stack_lock.stack_lock(directory=tmp_path):
        raise RuntimeError("step failed")
    with _stack_lock.stack_lock(directory=tmp_path) as again:
        assert again


def test_a_lock_file_another_user_made_read_only_can_still_be_taken(
    tmp_path: Path,
) -> None:
    lock_file = tmp_path / _stack_lock.LOCK_FILENAME
    lock_file.write_text("", encoding="utf-8")
    lock_file.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    with _stack_lock.stack_lock(directory=tmp_path) as held:
        assert held


def test_children_do_not_inherit_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemonised `docker` child holding the descriptor would keep the lock past the run."""
    opened: list[int] = []
    real_open = os.open

    def recording_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(_stack_lock.os, "open", recording_open)
    with _stack_lock.stack_lock(directory=tmp_path):
        assert [os.get_inheritable(fd) for fd in opened] == [False]
