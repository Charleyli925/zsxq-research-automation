"""Process-lifetime advisory locking for one local pipeline runtime."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager, ExitStack
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
    with ExitStack() as stack:
        if name == '.pipeline.lock' and stack.enter_context(held_lock(root / '.maintenance.lock')) is None:
            yield False
            return
        yield stack.enter_context(held_lock(path)) is not None


@contextmanager
def held_lock(path: Path, *, shared: bool = False):
    """Expose the descriptor so child processes can inherit writer exclusion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        # Closing, rather than LOCK_UN, preserves the lock while an inherited
        # descriptor still belongs to a child after a parent crash.
        yield handle


__all__ = ["runtime_lock"]
