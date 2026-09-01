from __future__ import annotations

from pathlib import Path
import tempfile

from agent_recovery import Runtime, Tool, UnknownOutcome


class FakeIssueAPI:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, str]] = {}
        self.create_calls = 0

    def create_issue(self, arguments: dict[str, str], key: str | None) -> dict[str, str]:
        if key is None:
            raise ValueError("the API requires an idempotency key")
        self.create_calls += 1
        issue = {"id": "issue-1", "title": arguments["title"]}
        self.issues[key] = issue
        raise UnknownOutcome("connection timed out after the server committed the issue")

    def find_issue(self, arguments: dict[str, str], key: str | None) -> dict[str, str] | None:
        return self.issues.get(key or "")


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "demo.db"
        api = FakeIssueAPI()
        runtime = Runtime(database)
        runtime.register(
            Tool(
                name="create_issue",
                execute=api.create_issue,
                inspect=api.find_issue,
            )
        )

        initial = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-report-123",
        )
        recovered = runtime.recover(initial.action_id)

        print(f"initial status:  {initial.status}")
        print(f"recovered status: {recovered.status}")
        print(f"issue:            {recovered.result}")
        print(f"create calls:     {api.create_calls}")
        runtime.close()


if __name__ == "__main__":
    main()
