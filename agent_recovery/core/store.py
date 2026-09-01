"""SQLite persistence for action attempts and lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from agent_recovery.core.actions import ActionStatus


@dataclass(frozen=True)
class StoredAction:
    action_id: str
    tool_name: str
    arguments: dict[str, Any]
    arguments_hash: str
    idempotency_key: str
    attempt: int
    status: ActionStatus
    result: Any = None
    error: str | None = None


class ActionStore:
    def __init__(self, database: str | Path) -> None:
        self._connection = sqlite3.connect(str(database))
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def create_action(
        self,
        *,
        action_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        arguments_hash: str,
        idempotency_key: str,
        attempt: int = 1,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO actions
                (action_id, tool_name, arguments_json, arguments_hash,
                 idempotency_key, attempt, status)
            VALUES (?, ?, ?, ?, ?, ?, 'running')
            """,
            (
                action_id,
                tool_name,
                _encode(arguments),
                arguments_hash,
                idempotency_key,
                attempt,
            ),
        )
        self._connection.commit()

    def find_latest(self, tool_name: str, idempotency_key: str) -> StoredAction | None:
        row = self._connection.execute(
            """
            SELECT * FROM actions
            WHERE tool_name = ? AND idempotency_key = ?
            ORDER BY attempt DESC
            LIMIT 1
            """,
            (tool_name, idempotency_key),
        ).fetchone()
        return _to_action(row) if row is not None else None

    def get(self, action_id: str) -> StoredAction | None:
        row = self._connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return _to_action(row) if row is not None else None

    def update(
        self,
        action_id: str,
        *,
        status: str,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        self._connection.execute(
            """
            UPDATE actions
            SET status = ?, result_json = ?, error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE action_id = ?
            """,
            (status, _encode(result) if result is not None else None, error, action_id),
        )
        self._connection.commit()

    def add_event(self, action_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._connection.execute(
            """
            INSERT INTO events (action_id, event_type, payload_json)
            VALUES (?, ?, ?)
            """,
            (action_id, event_type, _encode(payload)),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (tool_name, idempotency_key, attempt)
            );

            CREATE INDEX IF NOT EXISTS actions_latest_key
            ON actions (tool_name, idempotency_key, attempt DESC);

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL REFERENCES actions(action_id),
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self._connection.commit()


def _to_action(row: sqlite3.Row) -> StoredAction:
    return StoredAction(
        action_id=row["action_id"],
        tool_name=row["tool_name"],
        arguments=json.loads(row["arguments_json"]),
        arguments_hash=row["arguments_hash"],
        idempotency_key=row["idempotency_key"],
        attempt=row["attempt"],
        status=row["status"],
        result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
        error=row["error"],
    )


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
