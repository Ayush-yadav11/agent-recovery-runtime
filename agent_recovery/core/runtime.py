"""Runtime policy for registered side-effecting tools."""

from __future__ import annotations

import hashlib
from pathlib import Path
import secrets
from typing import Any

from agent_recovery.core.actions import ActionResult, ActionStatus, Tool, UnknownOutcome
from agent_recovery.core.store import ActionStore, StoredAction


class Runtime:
    """Execute tools with durable state and safe recovery semantics."""

    def __init__(self, database: str | Path = "agent_runs.db") -> None:
        self._store = ActionStore(database)
        self._tools: dict[str, Tool] = {}

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

        arguments_hash = _arguments_hash(arguments)
        existing = self._store.find_latest(tool_name, idempotency_key)
        if existing is not None:
            if existing.arguments_hash != arguments_hash:
                raise ValueError("idempotency key reused with different arguments")
            if existing.status in {"running", "success", "unknown"}:
                return _to_result(existing)

        action_id = secrets.token_hex(12)
        attempt = existing.attempt + 1 if existing is not None else 1
        self._store.create_action(
            action_id=action_id,
            tool_name=tool_name,
            arguments=arguments,
            arguments_hash=arguments_hash,
            idempotency_key=idempotency_key,
            attempt=attempt,
        )
        self._store.add_event(action_id, "action.started", {"tool_name": tool_name})

        try:
            value = tool.execute(arguments, idempotency_key)
        except UnknownOutcome as exc:
            self._finish(action_id, "unknown", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - tool boundaries must be contained
            self._finish(action_id, "failed", error=f"{type(exc).__name__}: {exc}")
        else:
            self._finish(action_id, "success", result=value)

        return self._result(action_id)

    def recover(self, action_id: str) -> ActionResult:
        row = self._get_action(action_id)
        if row.status != "unknown":
            raise ValueError("only unknown actions can be recovered")

        tool = self._tools.get(row.tool_name)
        if tool is None or tool.inspect is None:
            self._finish(
                action_id,
                "failed",
                error="no inspector is registered for this tool",
            )
            return self._result(action_id)

        try:
            value = tool.inspect(row.arguments, row.idempotency_key)
        except Exception as exc:  # noqa: BLE001 - inspection must not crash the runtime
            self._finish(
                action_id,
                "unknown",
                error=f"verification failed: {type(exc).__name__}: {exc}",
            )
        else:
            if value is None:
                self._finish(
                    action_id,
                    "failed",
                    error="verification did not find the expected side effect",
                )
            else:
                self._finish(action_id, "success", result=value)

        return self._result(action_id)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _finish(
        self,
        action_id: str,
        status: ActionStatus,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        self._store.update(action_id, status=status, result=result, error=error)
        payload = {"error": error} if error else {"result": result}
        self._store.add_event(action_id, f"action.{status}", payload)

    def _get_action(self, action_id: str) -> StoredAction:
        row = self._store.get(action_id)
        if row is None:
            raise KeyError(f"unknown action: {action_id}")
        return row

    def _result(self, action_id: str) -> ActionResult:
        return _to_result(self._get_action(action_id))


def _arguments_hash(arguments: dict[str, Any]) -> str:
    import json

    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_result(row: StoredAction) -> ActionResult:
    return ActionResult(
        action_id=row.action_id,
        tool_name=row.tool_name,
        status=row.status,
        result=row.result,
        error=row.error,
        attempt=row.attempt,
    )
