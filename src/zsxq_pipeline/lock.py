"""Process-lifetime advisory locking for one local pipeline runtime."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def runtime_lock(runtime_root: str | Path, *, name: str = ".pipeline.lock") -> Iterator[bool]:
    """Yield whether a non-blocking flock was acquired.

    The lock file is intentionally inert: it contains no PID, timestamp, or
    ownership claim to clean up.  Closing the descriptor (including process
    death) releases ``flock`` by kernel semantics, so a stale task cannot
    manufacture a permanent busy state.
    """

    root = Path(runtime_root).expanduser().resolve(strict=False)
    filename = str(name).strip()
    if not filename or Path(filename).name != filename:
        raise ValueError("lock name must be one simple filename")
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["runtime_lock"]
