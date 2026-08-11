"""Structured action logging for bu.

Each modifying action (backup, restore) writes JSON-lines entries to a
per-destination log file.  Non-modifying actions (check, verify, status,
config) are not logged.

Log entry format (one JSON object per line)::

    {"correlation_id":"<uuid>","action":"backup","destination":"photos",
     "timestamp":"2026-08-11T20:45:00Z","type":"start"}

    {"correlation_id":"<uuid>","action":"backup","destination":"photos",
     "timestamp":"2026-08-11T20:45:01Z","type":"error",
     "message":"File not found: /tmp/foo"}

    {"correlation_id":"<uuid>","action":"backup","destination":"photos",
     "timestamp":"2026-08-11T20:45:30Z","type":"end",
     "files_copied":25,"bytes_copied":118323}

``type`` is one of ``start``, ``end``, ``error``, ``note``.
"""

from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any


class ActionLogger:
    """Writes structured action log entries to a JSON-lines file.

    Thread-safe by virtue of append-only writes with short atomic lines.
    """

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._correlation_id = str(uuid.uuid4())
        self._action: str = ""
        self._destination: str = ""

    # -- public API -------------------------------------------------------

    def start(self, action: str, destination: str) -> None:
        """Record the start of a modifying action."""
        self._action = action
        self._destination = destination
        self._write({"type": "start"})

    def end(self, **extra: Any) -> None:
        """Record successful completion.  Extra kwargs become entry fields."""
        self._write({"type": "end", **extra})

    def error(self, message: str) -> None:
        """Record an error that occurred during the action."""
        self._write({"type": "error", "message": message})

    def note(self, message: str) -> None:
        """Record an informational note."""
        self._write({"type": "note", "message": message})

    # -- internals --------------------------------------------------------

    def _write(self, fields: dict[str, Any]) -> None:
        entry: dict[str, Any] = {
            "correlation_id": self._correlation_id,
            "action": self._action,
            "destination": self._destination,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        entry.update(fields)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")


def read_log(log_path: Path) -> list[dict[str, Any]]:
    """Parse all JSON-lines entries from a log file.

    Returns a list of entry dicts.  Corrupt lines are skipped.
    """
    entries: list[dict[str, Any]] = []
    if not log_path.exists():
        return entries

    with open(log_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def group_entries(
    entries: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group log entries by correlation_id.

    Returns ``{correlation_id: [entry, ...]}``, each list sorted by timestamp.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        cid = e.get("correlation_id", "")
        groups.setdefault(cid, []).append(e)
    for g in groups.values():
        g.sort(key=lambda e: e.get("timestamp", ""))
    return groups
