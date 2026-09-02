"""Read-only access to the action lifecycle event log.

The runtime writes one row per lifecycle event through
`ActionStore.add_event`. This module exposes those rows as an ordered,
typed log so an operator can reconstruct what happened during a recovery
without writing SQL.

Two properties of the underlying schema shape this API:

* Events are ordered by their autoincrementing `event_id`, not by their
  timestamp. `created_at` defaults to SQLite's `CURRENT_TIMESTAMP`, which
  has whole-second resolution, so the events of one action usually share
  a timestamp and cannot be ordered by it.
* The reader opens its own read-only connection to the same database
  file. It never writes and never migrates. A `:memory:` database cannot
  be shared between connections, so the runtime must be given a file path
  for its events to be readable here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_SELECT = """
SELECT action_id, event_type, payload_json, created_at
FROM events
"""


@dataclass(frozen=True)
class EventLogEntry:
    """One recorded lifecycle event.

    `timestamp` is the raw SQLite `CURRENT_TIMESTAMP` string in UTC, kept
    verbatim so the log reads exactly as it was stored. Use `moment` for
    the parsed value.
    """

    action_id: str
    event_type: str
    timestamp: str
    payload: dict[str, Any]

    @property
    def moment(self) -> datetime | None:
        """The timestamp as a UTC datetime, or `None` if it cannot be parsed."""
        return _parse_timestamp(self.timestamp)


class EventReader:
    """Read the event log of an action store without modifying it."""

    def __init__(self, database: str | Path) -> None:
        path = Path(database)
        if not path.is_file():
            raise FileNotFoundError(f"no action store at: {path}")
        self._connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._connection.row_factory = sqlite3.Row

    def events(self, action_id: str) -> list[EventLogEntry]:
        """Every event recorded for one action, oldest first."""
        rows = self._connection.execute(
            f"{_SELECT} WHERE action_id = ? ORDER BY event_id ASC",
            (action_id,),
        ).fetchall()
        return [_to_entry(row) for row in rows]

    def filter(self, event_types: set[str] | None = None) -> Iterator[EventLogEntry]:
        """Iterate every event across all actions, oldest first.

        With `event_types`, only those types are yielded. An empty set
        yields nothing, which keeps `filter(set())` distinct from
        `filter(None)`.
        """
        if event_types is not None and not event_types:
            return
        cursor = self._connection.execute(f"{_SELECT} ORDER BY event_id ASC")
        for row in cursor:
            entry = _to_entry(row)
            if event_types is None or entry.event_type in event_types:
                yield entry

    def action_ids(self) -> list[str]:
        """Every action that has at least one event, in first-event order."""
        rows = self._connection.execute(
            "SELECT action_id, MIN(event_id) AS first_event FROM events"
            " GROUP BY action_id ORDER BY first_event ASC"
        ).fetchall()
        return [row["action_id"] for row in rows]

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> EventReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _to_entry(row: sqlite3.Row) -> EventLogEntry:
    return EventLogEntry(
        action_id=row["action_id"],
        event_type=row["event_type"],
        timestamp=row["created_at"],
        payload=_decode(row["payload_json"]),
    )


def _decode(payload_json: str | None) -> dict[str, Any]:
    """Decode a stored payload without ever raising on unexpected content.

    Reading the log must not fail because one row holds something other
    than a JSON object, so a non-object payload is wrapped instead.
    """
    if payload_json is None:
        return {}
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return {"raw": payload_json}
    if isinstance(payload, dict):
        return payload
    return {"value": payload}


def _parse_timestamp(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    text = timestamp.strip()
    for fmt in (_TIMESTAMP_FORMAT, f"{_TIMESTAMP_FORMAT}.%f"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def seconds_between(start: EventLogEntry, end: EventLogEntry) -> float | None:
    """Seconds from `start` to `end`, or `None` if either timestamp is unusable."""
    first, last = start.moment, end.moment
    if first is None or last is None:
        return None
    return (last - first).total_seconds()


def entries_of_type(
    events: Iterable[EventLogEntry],
    event_types: set[str],
) -> list[EventLogEntry]:
    """The subset of `events` whose type is in `event_types`, order preserved."""
    return [event for event in events if event.event_type in event_types]
