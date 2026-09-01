"""Small, framework-agnostic runtime for safe agent tool actions."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Callable, Literal


ActionStatus = Literal["running", "success", "failed", "unknown"]
ExecuteFn = Callable[[dict[str, Any], str | None], Any]
InspectFn = Callable[[dict[str, Any], str | None], Any | None]


class UnknownOutcome(RuntimeError):
    """The external request may have succeeded, but its response was lost."""


@dataclass(frozen=True)
class Tool:
    """A side-effecting operation and its read-only state inspector."""

    name: str
    execute: ExecuteFn
    inspect: InspectFn | None = None


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    tool_name: str
    status: ActionStatus
    result: Any = None
    error: str | None = None


class Runtime:
    """Execute registered tools with durable state and safe recovery semantics."""

    def __init__(self, database: str | Path = "agent_runs.db") -> None:
        self._database = str(database)
        self._tools: dict[str, Tool] = {}
        self._connection = sqlite3.connect(self._database)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        if not idempotency_key:
            raise ValueError("idempotency_key is required for side-effecting actions")

        tool = self._tools.get(tool_name)
        if tool is None:
            raise KeyError(f"unknown tool: {tool_name}")

        existing = self._find_by_key(tool_name, idempotency_key)
        if existing is not None and existing["status"] in {"running", "success", "unknown"}:
            return self._to_result(existing)

        action_id = secrets.token_hex(12)
        self._connection.execute(
            """
            INSERT INTO actions
                (action_id, tool_name, arguments_json, idempotency_key, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (action_id, tool_name, _encode(arguments), idempotency_key),
        )
        self._event(action_id, "action.started", {"tool_name": tool_name})
        self._connection.commit()

        try:
            value = tool.execute(arguments, idempotency_key)
        except UnknownOutcome as exc:
            self._update_action(action_id, "unknown", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - tool boundaries must be contained
            self._update_action(action_id, "failed", error=f"{type(exc).__name__}: {exc}")
        else:
            self._update_action(action_id, "success", result=value)

        return self._get_result(action_id)

    def recover(self, action_id: str) -> ActionResult:
        row = self._get_row(action_id)
        if row["status"] != "unknown":
            raise ValueError("only unknown actions can be recovered")

        tool = self._tools.get(row["tool_name"])
        if tool is None or tool.inspect is None:
            self._update_action(
                action_id,
                "failed",
                error="no inspector is registered for this tool",
            )
            return self._get_result(action_id)

        arguments = json.loads(row["arguments_json"])
        try:
            value = tool.inspect(arguments, row["idempotency_key"])
        except Exception as exc:  # noqa: BLE001 - inspection must not crash the runtime
            self._update_action(
                action_id,
                "unknown",
                error=f"verification failed: {type(exc).__name__}: {exc}",
            )
        else:
            if value is None:
                self._update_action(
                    action_id,
                    "failed",
                    error="verification did not find the expected side effect",
                )
            else:
                self._update_action(action_id, "success", result=value)

        return self._get_result(action_id)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (tool_name, idempotency_key)
            );

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

    def _find_by_key(self, tool_name: str, idempotency_key: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM actions WHERE tool_name = ? AND idempotency_key = ?",
            (tool_name, idempotency_key),
        ).fetchone()

    def _get_row(self, action_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown action: {action_id}")
        return row

    def _get_result(self, action_id: str) -> ActionResult:
        return self._to_result(self._get_row(action_id))

    def _to_result(self, row: sqlite3.Row) -> ActionResult:
        return ActionResult(
            action_id=row["action_id"],
            tool_name=row["tool_name"],
            status=row["status"],
            result=_decode(row["result_json"]),
            error=row["error"],
        )

    def _update_action(
        self,
        action_id: str,
        status: ActionStatus,
        *,
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
        self._event(
            action_id,
            f"action.{status}",
            {"error": error} if error else {"result": result},
        )
        self._connection.commit()

    def _event(self, action_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._connection.execute(
            "INSERT INTO events (action_id, event_type, payload_json) VALUES (?, ?, ?)",
            (action_id, event_type, _encode(payload)),
        )


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: str | None) -> Any:
    return json.loads(value) if value is not None else None
