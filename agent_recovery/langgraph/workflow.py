"""Deterministic LangGraph workflow for safe tool recovery."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from agent_recovery import Runtime
from agent_recovery.langgraph.state import RecoveryState

ArgumentBuilder = Callable[[RecoveryState], dict[str, Any]]


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


def build_recovery_graph(
    runtime: Runtime,
    tool_name: str,
    arguments_builder: ArgumentBuilder | None = None,
    *,
    checkpointer: Any = None,
    interrupt_before: list[str] | None = None,
):
    """Build a compiled graph around one registered Runtime tool."""

    def build_arguments(state: RecoveryState) -> dict[str, Any]:
        if arguments_builder is not None:
            return arguments_builder(state)
        return {
            field: state[field]
            for field in ("title", "body")
            if field in state
        }

    def execute_action(state: RecoveryState) -> dict[str, Any]:
        result = runtime.execute(
            tool_name,
            build_arguments(state),
            idempotency_key=state["idempotency_key"],
        )
        return _result_state(result)

    def inspect_action(state: RecoveryState) -> dict[str, Any]:
        result = runtime.recover(state["action_id"])
        return _result_state(result)

    def retry_action(state: RecoveryState) -> dict[str, Any]:
        result = runtime.retry(state["action_id"])
        return _result_state(result)

    def success(state: RecoveryState) -> dict[str, Any]:
        return {"route": "success"}

    def failed(state: RecoveryState) -> dict[str, Any]:
        return {"route": "failed"}

    def human_review(state: RecoveryState) -> dict[str, Any]:
        return {"route": "human_review"}

    graph = StateGraph(RecoveryState)
    graph.add_node("execute_action", execute_action)
    graph.add_node("inspect_action", inspect_action)
    graph.add_node("retry_action", retry_action)
    graph.add_node("success", success)
    graph.add_node("failed", failed)
    graph.add_node("human_review", human_review)

    graph.add_edge(START, "execute_action")
    graph.add_conditional_edges(
        "execute_action",
        route_after_execute,
        {
            "success": "success",
            "verify": "inspect_action",
            "retry": "retry_action",
            "failed": "failed",
            "human_review": "human_review",
        },
    )
    graph.add_conditional_edges(
        "inspect_action",
        route_after_verify,
        {
            "success": "success",
            "retry": "retry_action",
            "human_review": "human_review",
        },
    )
    graph.add_conditional_edges(
        "retry_action",
        route_after_execute,
        {
            "success": "success",
            "verify": "inspect_action",
            "retry": "retry_action",
            "failed": "failed",
            "human_review": "human_review",
        },
    )

    for terminal in ("success", "failed", "human_review"):
        graph.add_edge(terminal, END)

    if checkpointer is not None:
        checkpointer.setup()
    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )


def _result_state(result: Any) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "action_id": result.action_id,
        "action_status": result.status,
        "action_result": result.result,
        "route": result.status,
    }
    if result.error is not None:
        updates["error"] = result.error
    return updates
