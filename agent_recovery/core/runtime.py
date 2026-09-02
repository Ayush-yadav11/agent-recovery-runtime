"""Runtime policy for registered side-effecting tools."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any

from agent_recovery.core.actions import (
    ActionResult,
    ActionStatus,
    RetryApproval,
    Tool,
    UnknownOutcome,
    VerificationOutcome,
    VerificationStatus,
)
from agent_recovery.core.store import ActionStore, StoredAction, StoredApproval

_ABSENT_REASON = "verification did not find the expected side effect"
_DEFERRED = frozenset({VerificationStatus.UNAVAILABLE, VerificationStatus.AMBIGUOUS})


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
            if existing.status == "failed":
                raise ValueError("action failed; use a new idempotency key")
            if existing.status == "verified_absent":
                raise ValueError("action is verified_absent; use retry")
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
        self._run_tool(action_id, tool, arguments, idempotency_key)
        return self._result(action_id)

    def request_retry_approval(self, action_id: str) -> RetryApproval:
        row = self._get_action(action_id)
        self._validate_latest_absence(row)
        existing = self._store.get_approval(action_id)
        if existing is not None:
            if existing.status == "consumed":
                raise ValueError("retry approval already consumed")
            return _to_approval(existing)

        approval = self._store.create_approval(secrets.token_hex(12), action_id)
        self._store.add_event(
            action_id,
            "approval.requested",
            {"approval_id": approval.approval_id},
        )
        return _to_approval(approval)

    def get_retry_approval(self, action_id: str) -> RetryApproval:
        approval = self._store.get_approval(action_id)
        if approval is None:
            raise KeyError(f"no retry approval for action: {action_id}")
        return _to_approval(approval)

    def approve_retry(self, action_id: str, *, reviewer: str, reason: str) -> RetryApproval:
        return self._decide_retry(action_id, "approved", reviewer, reason)

    def reject_retry(self, action_id: str, *, reviewer: str, reason: str) -> RetryApproval:
        return self._decide_retry(action_id, "rejected", reviewer, reason)

    def _decide_retry(
        self,
        action_id: str,
        status: str,
        reviewer: str,
        reason: str,
    ) -> RetryApproval:
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        if not reason.strip():
            raise ValueError("reason is required")
        row = self._get_action(action_id)
        self._validate_latest_absence(row)
        try:
            approval = self._store.decide_approval(
                action_id,
                status=status,  # type: ignore[arg-type]
                reviewer=reviewer,
                reason=reason,
            )
        except ValueError:
            raise ValueError("retry approval is not pending") from None
        self._store.add_event(
            action_id,
            f"approval.{status}",
            {
                "approval_id": approval.approval_id,
                "reviewer": reviewer,
                "reason": reason,
            },
        )
        return _to_approval(approval)

    def retry(self, action_id: str) -> ActionResult:
        row = self._get_action(action_id)
        approval = self._store.get_approval(action_id)
        if approval is None or approval.status == "pending":
            raise ValueError("retry requires an approved approval")
        if approval.status == "rejected":
            raise ValueError("retry approval is rejected")
        if approval.status == "consumed":
            raise ValueError("retry approval already consumed")
        self._validate_latest_absence(row)

        tool = self._tools.get(row.tool_name)
        if tool is None:
            raise KeyError(f"unknown tool: {row.tool_name}")

        new_action_id = secrets.token_hex(12)
        started = self._store.start_approved_retry(
            approval_action_id=action_id,
            action_id=new_action_id,
            tool_name=row.tool_name,
            arguments=row.arguments,
            arguments_hash=row.arguments_hash,
            idempotency_key=row.idempotency_key,
            attempt=row.attempt + 1,
        )
        if not started:
            raise ValueError("retry approval already consumed")
        self._store.add_event(
            action_id,
            "approval.consumed",
            {"approval_id": approval.approval_id, "retry_action_id": new_action_id},
        )
        self._store.add_event(
            new_action_id,
            "action.started",
            {"tool_name": row.tool_name, "retry_of": action_id},
        )
        self._run_tool(new_action_id, tool, row.arguments, row.idempotency_key)
        return self._result(new_action_id)

    def recover(self, action_id: str) -> ActionResult:
        row = self._get_action(action_id)
        if row.status not in {"unknown", "running"}:
            raise ValueError("only unknown or running actions can be recovered")
        if row.status == "running":
            self._finish(
                action_id,
                "unknown",
                error="execution may have been interrupted; verification required",
            )
            row = self._get_action(action_id)

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
            detail = f"{type(exc).__name__}: {exc}"
            outcome = self._classify_inspection_error(action_id, tool, exc)
            if outcome is None:
                self._record_inspection_error(action_id, detail)
            else:
                self._record_verification(action_id, outcome.with_detail(detail))
        else:
            self._record_verification(action_id, _outcome_from_value(value))

        return self._result(action_id)

    def _classify_inspection_error(
        self,
        action_id: str,
        tool: Tool,
        exc: BaseException,
    ) -> VerificationOutcome | None:
        """Ask the tool whether an inspection failure defers the verdict.

        Returns the deferring outcome, or `None` to keep the conservative
        default: an unclassified failure asserts nothing about the side effect.
        A tool without a classifier, a classifier that declines, a broken
        classifier and a classifier that oversteps all take that default.
        """
        if tool.classify is None:
            return None

        try:
            outcome = tool.classify(exc)
        except Exception as inner:  # noqa: BLE001 - a broken classifier cannot crash
            self._store.add_event(
                action_id,
                "verification.classifier_failed",
                {"error": f"{type(inner).__name__}: {inner}"},
            )
            return None

        if outcome is None:
            return None
        if outcome.status not in _DEFERRED:
            # An exception is not evidence: it can neither find the side effect
            # nor prove it absent, so a classifier may only defer the verdict.
            self._store.add_event(
                action_id,
                "verification.classifier_rejected",
                {"status": outcome.status.value},
            )
            return None
        return outcome

    def _record_verification(self, action_id: str, outcome: VerificationOutcome) -> None:
        """Apply one inspection outcome to the action and record the event."""
        if outcome.status is VerificationStatus.FOUND:
            self._finish(action_id, "success", result=outcome.value)
            payload: dict[str, Any] = {"result": outcome.value}
        elif outcome.status is VerificationStatus.VERIFIED_ABSENT:
            reason = outcome.reason or _ABSENT_REASON
            self._finish(action_id, "verified_absent", error=reason)
            payload = {"reason": reason}
        else:
            # `unavailable` and `ambiguous` answer nothing about the side
            # effect. The action stays `unknown`, so no retry is unlocked and
            # the operator decides when to inspect again.
            self._finish(
                action_id,
                "unknown",
                error=f"verification {outcome.status.value}: {outcome.reason}",
            )
            payload = {"reason": outcome.reason}
        self._store.add_event(action_id, f"verification.{outcome.status.value}", payload)

    def _record_inspection_error(self, action_id: str, detail: str) -> None:
        """Record an unclassified inspection failure without a verdict."""
        self._finish(action_id, "unknown", error=f"verification failed: {detail}")
        self._store.add_event(action_id, "verification.error", {"error": detail})

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _run_tool(
        self,
        action_id: str,
        tool: Tool,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        try:
            value = tool.execute(arguments, idempotency_key)
        except UnknownOutcome as exc:
            self._finish(action_id, "unknown", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - tool boundaries must be contained
            self._finish(action_id, "failed", error=f"{type(exc).__name__}: {exc}")
        else:
            self._finish(action_id, "success", result=value)

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

    def _validate_latest_absence(self, row: StoredAction) -> None:
        latest = self._store.find_latest(row.tool_name, row.idempotency_key)
        if latest is None or latest.action_id != row.action_id:
            raise ValueError("only the latest verified_absent action can be used")
        if row.status != "verified_absent":
            raise ValueError("only verified_absent actions can be retried")

    def _get_action(self, action_id: str) -> StoredAction:
        row = self._store.get(action_id)
        if row is None:
            raise KeyError(f"unknown action: {action_id}")
        return row

    def _result(self, action_id: str) -> ActionResult:
        return _to_result(self._get_action(action_id))


def _outcome_from_value(value: Any) -> VerificationOutcome:
    """Normalise an inspector's return value onto an explicit outcome.

    Tools may return a `VerificationOutcome` directly, or keep the legacy
    contract where the found object means found and `None` means absent.
    """
    if isinstance(value, VerificationOutcome):
        return value
    if value is None:
        return VerificationOutcome.verified_absent(_ABSENT_REASON)
    return VerificationOutcome.found(value)


def _arguments_hash(arguments: dict[str, Any]) -> str:
    import json

    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_approval(row: StoredApproval) -> RetryApproval:
    return RetryApproval(
        approval_id=row.approval_id,
        action_id=row.action_id,
        status=row.status,
        reviewer=row.reviewer,
        reason=row.reason,
    )


def _to_result(row: StoredAction) -> ActionResult:
    return ActionResult(
        action_id=row.action_id,
        tool_name=row.tool_name,
        status=row.status,
        result=row.result,
        error=row.error,
        attempt=row.attempt,
    )
