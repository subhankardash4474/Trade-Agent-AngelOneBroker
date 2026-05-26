"""Cross-platform advisory file lock for read-modify-write JSON state.

B-8 / C-29 (audit 2026-05-25 / 2026-05-26): the alert dedup state file
(`logs/alert_dedup_state.json`) and the three JSON persistence stores
(cooldowns, runtime_state, trailing_stop) all use the same
read-modify-write pattern. Atomic write via temp+rename keeps the file
on disk consistent, but it does NOT prevent the classic lost-update
problem when two processes overlap:

    A: read  state = {x: 1}
    B: read  state = {x: 1}
    A: write state = {x: 1, y: 2}
    B: write state = {x: 1, z: 3}   # A's y=2 is LOST

The agent normally enforces single-instance via `daemon.pid`, but the
race window opens on every container restart (old container draining
state while new one boots) and on every battery worker (multiple
processes touching the same `data/cache/`). This helper provides a
portable advisory lock — `msvcrt.locking` on Windows, `fcntl.flock`
on POSIX — that callers wrap around the RMW block.

Contract
========
* `with file_lock(path, timeout=N):` ... blocking, raises TimeoutError
  on timeout. `path` is the FILE to be locked (usually the same JSON
  file the caller is about to read/modify/write). On the rare platform
  that supports neither `fcntl` nor `msvcrt`, the lock is a no-op
  (best-effort) and a single warning is emitted at import time.
* The lock is advisory — only respected by callers that also use this
  helper. That's fine for our use case (we own every reader/writer).
* Lock file is the same path as the data file. We open it with O_RDWR
  and never truncate, so the lock doesn't disturb the contents.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Iterator, Union

from loguru import logger


_HAS_FCNTL = False
_HAS_MSVCRT = False

try:
    import fcntl as _fcntl  # type: ignore[import-not-found]
    _HAS_FCNTL = True
except ImportError:
    try:
        import msvcrt as _msvcrt  # type: ignore[import-not-found]
        _HAS_MSVCRT = True
    except ImportError:
        logger.warning(
            "[file_lock] Neither fcntl (POSIX) nor msvcrt (Windows) is "
            "available; file_lock() will be a no-op. Cross-process race "
            "protection for RMW JSON state is DISABLED on this platform."
        )


@contextlib.contextmanager
def file_lock(path: Union[str, Path], *, timeout: float = 5.0) -> Iterator[None]:
    """Acquire an exclusive advisory lock keyed off `path`; release on exit.

    Internally locks a sibling `.lock` file (NOT the data file itself).
    This matters on Windows: holding an open handle on the data file
    blocks `os.replace(tmp, data_file)`, which is exactly the atomic
    write pattern every caller uses. Locking a sibling file leaves the
    data file untouched and writable.

    Polls every 50ms until acquired or `timeout` expires. Raises
    TimeoutError on expiry.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    # Open in read+write mode without truncating. Caller is responsible
    # for any actual writes through their own atomic-write helper; this
    # fd exists solely for the lock.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        if _HAS_FCNTL:
            while True:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    break
                except BlockingIOError as err:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"file_lock({path}) timed out after {timeout}s"
                        ) from err
                    time.sleep(0.05)
        elif _HAS_MSVCRT:
            # msvcrt.locking only locks the specified byte range. Lock a
            # single byte at offset 0 — fine for our advisory use.
            while True:
                try:
                    _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                    break
                except OSError as err:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"file_lock({path}) timed out after {timeout}s"
                        ) from err
                    time.sleep(0.05)
        # else: no-op (warned at import time)
        yield
    finally:
        try:
            if _HAS_FCNTL:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _HAS_MSVCRT:
                try:
                    # Rewind: msvcrt.locking unlocks from current position
                    os.lseek(fd, 0, os.SEEK_SET)
                    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(fd)


__all__ = ["file_lock"]
