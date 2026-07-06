"""In-memory status tracker for an in-flight "Run now", so the web UI can poll for a
live status message while the request that's actually doing the scan is still open.
Not persisted -- fine, since it only ever describes a run that's happening right now;
a restart mid-run just means the poller sees nothing until the next run starts.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_status: dict[int, str] = {}


def set_status(watch_id: int, message: str) -> None:
    with _lock:
        _status[watch_id] = message


def get_status(watch_id: int) -> str | None:
    with _lock:
        return _status.get(watch_id)


def clear_status(watch_id: int) -> None:
    with _lock:
        _status.pop(watch_id, None)
