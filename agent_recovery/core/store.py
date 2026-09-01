"""SQLite persistence for action attempts and lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from agent_recovery.core.actions import ActionStatus


_CREATE_ACTIONS = """
CREATE TABLE actions (
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
)
"""


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
        status: ActionStatus,
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
        action_table = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'actions'"
        ).fetchone()
        if action_table is None:
            self._connection.executescript(_CREATE_ACTIONS)
        else:
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(actions)")
            }
            if {"arguments_hash", "attempt"} - columns:
                self._migrate_legacy_actions()

        self._connection.executescript(
            """
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

    def _migrate_legacy_actions(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = OFF")
        legacy_rows = self._connection.execute(
            """
            SELECT action_id, tool_name, arguments_json, idempotency_key,
                   status, result_json, error, created_at, updated_at
            FROM actions
            """
        ).fetchall()
        self._connection.execute("ALTER TABLE actions RENAME TO actions_legacy")
        self._connection.executescript(_CREATE_ACTIONS)
        for row in legacy_rows:
            arguments = json.loads(row["arguments_json"])
            self._connection.execute(
                """
                INSERT INTO actions
                    (action_id, tool_name, arguments_json, arguments_hash,
                     idempotency_key, attempt, status, result_json, error,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    row["action_id"],
                    row["tool_name"],
                    row["arguments_json"],
                    _arguments_hash(arguments),
                    row["idempotency_key"],
                    row["status"],
                    row["result_json"],
                    row["error"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        self._connection.execute("DROP TABLE actions_legacy")


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


def _arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(_encode(arguments).encode("utf-8")).hexdigest()


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
