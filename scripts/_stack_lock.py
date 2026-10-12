#  Project:      dfe-docker
#  File:         scripts/_stack_lock.py
#  Purpose:      One advisory lock that serialises everything which takes the stack down
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checkout's stack lock, shared by ``make proof`` and the daemon updater.

``make proof`` tears the standing stack down and rebuilds it per e2e test, and an
update tick runs ``make ci``, whose ``down`` removes the stack the proof is
testing. Both take this lock for their whole run, so only one of them touches the
stack at a time.

The lock is ``flock(2)`` on a file in the checkout. The kernel drops it when the
holder's descriptor closes, so a crashed or killed holder never leaves it stuck,
and it needs no PID bookkeeping. The descriptor is not inheritable, so the
``make`` and ``docker`` children a holder spawns never keep it alive.

Linux and macOS only (``fcntl``); the callers already are.
"""

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

LOCK_FILENAME = ".dfe-stack.lock"


@contextlib.contextmanager
def stack_lock(*, directory: Path) -> Iterator[bool]:
    """Hold the checkout's stack lock; yield False at once when someone else does.

    The file opens read-only (creating it when absent): ``flock`` needs no write
    access, so a user who cannot write a lock file another user created can still
    take the lock.

    Args:
        directory: The dfe-docker checkout the lock file lives in.

    Yields:
        True while this process holds the lock, False when another holder has it.
    """
    descriptor = os.open(directory / LOCK_FILENAME, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)
