"""File reads that cannot hang the process.

Motivation, found the hard way: a LaunchAgent running this tool blocked forever
inside `open()` on a path under `~/Documents`. macOS TCC protects that location,
and a background agent has no way to show the consent prompt, so the syscall
never returns. `ls` on the same path fails fast with EPERM, but an explicit
`open()` hangs - so "check first, then read" is not enough either.

Any path that might sit outside the agent-config directories is therefore read
through here: the read happens on a daemon thread and the caller gives up after a
timeout. A path that times out is remembered, so one unreadable location costs
one timeout per run rather than one per check.

The trade-off is explicit: a hung thread is leaked until the process exits. That
is acceptable because the alternative is the whole run hanging, and the daemon
flag keeps it from holding up interpreter shutdown.
"""

from __future__ import annotations

import os
import threading

#: Seconds to wait for a single read before declaring the path unreadable.
DEFAULT_TIMEOUT = 3.0

_unreadable: set[str] = set()
_lock = threading.Lock()


def known_unreadable() -> set[str]:
    """Paths that timed out earlier in this process."""
    with _lock:
        return set(_unreadable)


def reset() -> None:
    """Forget the unreadable set. Used by tests."""
    with _lock:
        _unreadable.clear()


def read_bytes(path: str, timeout: float = DEFAULT_TIMEOUT) -> bytes | None:
    """Read ``path``, or return None if it is missing, denied, or unresponsive."""
    with _lock:
        if path in _unreadable:
            return None

    result: dict[str, bytes | None] = {}

    def worker() -> None:
        try:
            with open(path, "rb") as fh:
                result["data"] = fh.read()
        except OSError:
            result["data"] = None

    t = threading.Thread(target=worker, daemon=True, name=f"safeio:{os.path.basename(path)}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        with _lock:
            _unreadable.add(path)
        return None
    return result.get("data")


def read_text(path: str, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    data = read_bytes(path, timeout)
    if data is None:
        return None
    return data.decode("utf-8", "replace")


def exists(path: str, timeout: float = DEFAULT_TIMEOUT) -> bool | None:
    """True/False when determinable, None when the check itself is unresponsive."""
    with _lock:
        if path in _unreadable:
            return None

    result: dict[str, bool] = {}

    def worker() -> None:
        try:
            result["ok"] = os.path.exists(path)
        except OSError:
            result["ok"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        with _lock:
            _unreadable.add(path)
        return None
    return result.get("ok", False)
