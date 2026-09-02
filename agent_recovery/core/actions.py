"""Domain types for side-effecting agent actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal


ActionStatus = Literal[
    "running",
    "success",
    "failed",
    "unknown",
    "verified_absent",
]
ApprovalStatus = Literal["pending", "approved", "rejected", "consumed"]
ExecuteFn = Callable[[dict[str, Any], str | None], Any]
InspectFn = Callable[[dict[str, Any], str | None], Any | None]


@dataclass(frozen=True)
class RetryApproval:
    approval_id: str
    action_id: str
    status: ApprovalStatus
    reviewer: str | None = None
    reason: str | None = None


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
    attempt: int = 1
