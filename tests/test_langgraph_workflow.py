import unittest
from tempfile import TemporaryDirectory
from typing import Any

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver

from agent_recovery import Runtime, Tool, UnknownOutcome
from agent_recovery.integrations.github import GitHubClient
from agent_recovery.langgraph.state import RecoveryState
from agent_recovery.langgraph.workflow import (
    build_recovery_graph,
    route_after_execute,
    route_after_verify,
)


class RoutingTests(unittest.TestCase):
    def test_execute_routes_success_to_success(self) -> None:
        self.assertEqual(
            route_after_execute(RecoveryState(action_status="success")),
            "success",
        )

    def test_execute_routes_unknown_to_verify(self) -> None:
        self.assertEqual(
            route_after_execute(RecoveryState(action_status="unknown")),
            "verify",
        )

    def test_execute_routes_verified_absent_to_retry(self) -> None:
        self.assertEqual(
            route_after_execute(RecoveryState(action_status="verified_absent")),
            "retry",
        )

    def test_execute_routes_failed_to_failed(self) -> None:
        self.assertEqual(
            route_after_execute(RecoveryState(action_status="failed")),
            "failed",
        )

    def test_execute_routes_missing_status_to_human_review(self) -> None:
        self.assertEqual(route_after_execute(RecoveryState()), "human_review")

    def test_verify_routes_success_to_success(self) -> None:
        self.assertEqual(
            route_after_verify(RecoveryState(action_status="success")),
            "success",
        )

    def test_verify_routes_verified_absent_to_retry(self) -> None:
        self.assertEqual(
            route_after_verify(RecoveryState(action_status="verified_absent")),
            "retry",
        )

    def test_verify_routes_unknown_to_human_review(self) -> None:
        self.assertEqual(
            route_after_verify(RecoveryState(action_status="unknown")),
            "human_review",
        )

    def test_verify_routes_failed_to_human_review(self) -> None:
        self.assertEqual(
            route_after_verify(RecoveryState(action_status="failed")),
            "human_review",
        )


class GraphExecutionTests(unittest.TestCase):
    def test_github_client_runs_through_unknown_inspect_and_retry(self) -> None:
        requests: list[httpx.Request] = []
        post_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal post_calls
            requests.append(request)
            if request.method == "POST":
                post_calls += 1
                if post_calls == 1:
                    raise httpx.ReadTimeout("response lost", request=request)
                return httpx.Response(201, json={"id": 42}, request=request)
            return httpx.Response(200, json=[], request=request)

        client = GitHubClient("test-token", transport=httpx.MockTransport(handler))
        with TemporaryDirectory() as directory:
            runtime = Runtime(f"{directory}/actions.db")
            runtime.register(
                Tool(
                    name="github.create_issue",
                    execute=lambda arguments, key: client.create_issue(
                        arguments["owner"],
                        arguments["repository"],
                        arguments["title"],
                        arguments["body"],
                        key,
                    ),
                    inspect=lambda arguments, key: client.find_issue_by_idempotency_key(
                        arguments["owner"], arguments["repository"], key
                    ),
                )
            )
            graph = build_recovery_graph(
                runtime,
                "github.create_issue",
                arguments_builder=lambda state: {
                    "owner": state["owner"],
                    "repository": state["repository"],
                    "title": state["title"],
                    "body": state["body"],
                },
            )
            result = graph.invoke(
                {
                    "owner": "owner",
                    "repository": "repo",
                    "title": "Login is broken",
                    "body": "Customer report",
                    "idempotency_key": "customer-123",
                }
            )
            runtime.close()
        client.close()

        self.assertEqual(result["action_status"], "success")
        self.assertEqual(post_calls, 2)
        self.assertEqual(len(requests), 3)

    def test_graph_executes_a_successful_tool(self) -> None:
        calls: list[dict[str, Any]] = []

        def create_issue(arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
            calls.append({"arguments": arguments, "key": key})
            return {"id": "issue-1", "title": arguments["title"]}

        with TemporaryDirectory() as directory:
            runtime = Runtime(f"{directory}/actions.db")
            runtime.register(Tool(name="create_issue", execute=create_issue))
            graph = build_recovery_graph(runtime, "create_issue")

            result = graph.invoke(
                {
                    "title": "Login is broken",
                    "body": "Customer report",
                    "idempotency_key": "customer-123",
                }
            )
            runtime.close()

        self.assertEqual(result["route"], "success")
        self.assertEqual(result["action_status"], "success")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["title"], "Login is broken")

    def test_graph_recovers_unknown_without_repeating_execute(self) -> None:
        calls = 0
        issue = {"id": "issue-1", "title": "Login is broken"}

        def create_issue(arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            raise UnknownOutcome("response lost after commit")

        def inspect_issue(arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
            return issue

        with TemporaryDirectory() as directory:
            runtime = Runtime(f"{directory}/actions.db")
            runtime.register(
                Tool(
                    name="create_issue",
                    execute=create_issue,
                    inspect=inspect_issue,
                )
            )
            graph = build_recovery_graph(runtime, "create_issue")
            result = graph.invoke(
                {
                    "title": issue["title"],
                    "body": "Customer report",
                    "idempotency_key": "customer-123",
                }
            )
            runtime.close()

        self.assertEqual(result["route"], "success")
        self.assertEqual(result["action_status"], "success")
        self.assertEqual(calls, 1)

    def test_graph_resumes_after_unknown_from_persistent_checkpoint(self) -> None:
        calls = 0
        issue = {"id": "issue-1", "title": "Login is broken"}

        def create_issue(arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            raise UnknownOutcome("response lost after commit")

        def inspect_issue(arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
            return issue

        with TemporaryDirectory() as directory:
            actions_database = f"{directory}/actions.db"
            checkpoints_database = f"{directory}/checkpoints.db"
            config = {"configurable": {"thread_id": "customer-123"}}
            runtime = Runtime(actions_database)
            runtime.register(
                Tool(
                    name="create_issue",
                    execute=create_issue,
                    inspect=inspect_issue,
                )
            )

            with SqliteSaver.from_conn_string(checkpoints_database) as checkpointer:
                interrupted_graph = build_recovery_graph(
                    runtime,
                    "create_issue",
                    checkpointer=checkpointer,
                    interrupt_before=["inspect_action"],
                )
                interrupted = interrupted_graph.invoke(
                    {
                        "title": issue["title"],
                        "body": "Customer report",
                        "idempotency_key": "customer-123",
                    },
                    config,
                )

                self.assertEqual(interrupted["action_status"], "unknown")
                runtime.close()

            restarted_runtime = Runtime(actions_database)
            restarted_runtime.register(
                Tool(
                    name="create_issue",
                    execute=create_issue,
                    inspect=inspect_issue,
                )
            )
            with SqliteSaver.from_conn_string(checkpoints_database) as checkpointer:
                restarted_graph = build_recovery_graph(
                    restarted_runtime,
                    "create_issue",
                    checkpointer=checkpointer,
                )
                resumed = restarted_graph.invoke(None, config)
            restarted_runtime.close()

        self.assertEqual(resumed["route"], "success")
        self.assertEqual(resumed["action_status"], "success")
        self.assertEqual(calls, 1)

    def test_graph_retries_only_after_verified_absence(self) -> None:
        calls = 0

        def create_issue(arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise UnknownOutcome("response lost before commit")
            return {"id": "issue-2", "title": arguments["title"]}

        def inspect_issue(arguments: dict[str, Any], key: str | None) -> None:
            return None

        with TemporaryDirectory() as directory:
            runtime = Runtime(f"{directory}/actions.db")
            runtime.register(
                Tool(
                    name="create_issue",
                    execute=create_issue,
                    inspect=inspect_issue,
                )
            )
            graph = build_recovery_graph(runtime, "create_issue")
            result = graph.invoke(
                {
                    "title": "Login is broken",
                    "body": "Customer report",
                    "idempotency_key": "customer-456",
                }
            )
            runtime.close()

        self.assertEqual(result["route"], "success")
        self.assertEqual(result["action_status"], "success")
        self.assertEqual(calls, 2)

