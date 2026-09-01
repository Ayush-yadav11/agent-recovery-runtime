"""Deterministic routing for the recovery workflow."""

from __future__ import annotations

from agent_recovery.langgraph.state import RecoveryState


def route_after_execute(state: RecoveryState) -> str:
    status = state.get("action_status")
    if status == "success":
        return "success"
    if status == "unknown":
        return "verify"
    if status == "verified_absent":
        return "retry"
    if status == "failed":
        return "failed"
    return "human_review"


def route_after_verify(state: RecoveryState) -> str:
    status = state.get("action_status")
    if status == "success":
        return "success"
    if status == "verified_absent":
        return "retry"
    return "human_review"
