"""Run the recovery workflow against a GitHub-like fake API."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from agent_recovery import Runtime, Tool, UnknownOutcome
from agent_recovery.langgraph.workflow import build_recovery_graph


class FakeIssueAPI:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, str]] = {}
        self.create_calls = 0
        self.inspect_calls = 0

    def create_issue(self, arguments: dict[str, Any], key: str) -> dict[str, str]:
        self.create_calls += 1
        self.issues[key] = {"id": "issue-1", "title": arguments["title"]}
        raise UnknownOutcome("response lost after the server committed the issue")

    def find_issue(self, arguments: dict[str, Any], key: str) -> dict[str, str] | None:
        self.inspect_calls += 1
        return self.issues.get(key)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "demo.db"
        api = FakeIssueAPI()
        with Runtime(database) as runtime:
            runtime.register(
                Tool(
                    name="create_issue",
                    execute=api.create_issue,
                    inspect=api.find_issue,
                )
            )
            graph = build_recovery_graph(runtime, "create_issue")
            result = graph.invoke(
                {
                    "title": "Login is broken",
                    "body": "Customer report",
                    "idempotency_key": "customer-report-123",
                }
            )

        print(f"final status:     {result['action_status']}")
        print(f"verified result:  {result['action_result']}")
        print(f"create calls:     {api.create_calls}")
        print(f"inspect calls:    {api.inspect_calls}")


if __name__ == "__main__":
    main()
