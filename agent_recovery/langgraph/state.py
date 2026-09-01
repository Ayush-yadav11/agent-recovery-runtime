"""State carried by the LangGraph recovery workflow."""

from __future__ import annotations

from typing import Any, Literal
from typing_extensions import TypedDict


class RecoveryState(TypedDict, total=False):
    owner: str
    repository: str
    title: str
    body: str
    idempotency_key: str
    action_id: str
    action_status: Literal[
        "running",
        "success",
        "failed",
        "unknown",
        "verified_absent",
    ]
    action_result: Any
    error: str
    route: Literal["success", "verify", "retry", "await_retry", "human_review", "failed"]
