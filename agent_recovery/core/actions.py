"""Domain types for side-effecting agent actions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
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


class VerificationStatus(str, Enum):
    """What a read-only inspection concluded about a side effect."""

    FOUND = "found"
    VERIFIED_ABSENT = "verified_absent"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"


_REASON_REQUIRED = frozenset(
    {VerificationStatus.UNAVAILABLE, VerificationStatus.AMBIGUOUS}
)


@dataclass(frozen=True)
class VerificationOutcome:
    """The result of inspecting an external system for one side effect.

    Absence and inspection failure are different facts. `verified_absent`
    asserts the side effect does not exist; `unavailable` and `ambiguous`
    assert only that the question could not be answered, so the action must
    stay `unknown` instead of becoming eligible for retry.
    """

    status: VerificationStatus
    value: Any = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is VerificationStatus.FOUND and self.value is None:
            raise ValueError("a found outcome requires the inspected value")
        if self.status in _REASON_REQUIRED and not (self.reason or "").strip():
            raise ValueError(f"an {self.status.value} outcome requires a reason")

    @classmethod
    def found(cls, value: Any) -> VerificationOutcome:
        """The side effect exists in the external system."""
        return cls(VerificationStatus.FOUND, value=value)

    @classmethod
    def verified_absent(cls, reason: str | None = None) -> VerificationOutcome:
        """The external system was queried and holds no matching side effect."""
        return cls(VerificationStatus.VERIFIED_ABSENT, reason=reason)

    @classmethod
    def unavailable(cls, reason: str) -> VerificationOutcome:
        """The external system could not be queried; ask again later."""
        return cls(VerificationStatus.UNAVAILABLE, reason=reason)

    @classmethod
    def ambiguous(cls, reason: str) -> VerificationOutcome:
        """The query returned inconclusive data; a human must decide."""
        return cls(VerificationStatus.AMBIGUOUS, reason=reason)

    def with_detail(self, detail: str) -> VerificationOutcome:
        """Attach the underlying inspection error to the recorded reason."""
        if not detail:
            return self
        if not self.reason:
            return replace(self, reason=detail)
        return replace(self, reason=f"{self.reason} ({detail})")


ClassifyFn = Callable[[BaseException], "VerificationOutcome | None"]


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
    """A side-effecting operation and its read-only state inspector.

    `inspect` may return a `VerificationOutcome`, or keep the simpler legacy
    contract of the found object or `None` for absence. `classify` maps an
    inspection exception onto `VerificationOutcome.unavailable` or
    `VerificationOutcome.ambiguous`; without it every inspection exception
    leaves the action `unknown`.
    """

    name: str
    execute: ExecuteFn
    inspect: InspectFn | None = None
    classify: ClassifyFn | None = None


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    tool_name: str
    status: ActionStatus
    result: Any = None
    error: str | None = None
    attempt: int = 1
