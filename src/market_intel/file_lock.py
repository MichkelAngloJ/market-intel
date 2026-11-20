"""A cross-process lock on a file, standard library only.

    with file_lock(path):
        ...  # only one process (and one thread) at a time

Windows uses ``msvcrt.locking`` and other systems ``fcntl.flock``. Both are released by the
operating system if the process dies, so a crash can't leave a stale lock behind. The lock
file itself stays (empty); never delete it while the lock may be in use.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

DEFAULT_TIMEOUT_S = 10.0
POLL_S = 0.02


def _try_lock(fh: BinaryIO) -> bool:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # one byte; may be past EOF
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(
    path: Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    clock=time.monotonic,
    sleep=time.sleep,
) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the block. Raises TimeoutError after ``timeout_s``.

    Each call opens its own handle, so two threads of one process exclude each other too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+b")
    try:
        deadline = clock() + timeout_s
        while not _try_lock(fh):
            if clock() >= deadline:
                raise TimeoutError(f"could not lock {path.name} within {timeout_s:g} s")
            sleep(POLL_S)
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()
