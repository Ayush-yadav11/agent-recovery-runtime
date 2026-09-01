import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_recovery import Runtime, Tool, UnknownOutcome


class FakeSideEffect:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.issues: dict[str, dict[str, Any]] = {}
        self.create_calls = 0
        self.inspect_calls = 0

    def execute(self, arguments: dict[str, Any], key: str) -> dict[str, Any]:
        self.create_calls += 1
        if self.mode == "before_commit":
            raise RuntimeError("request rejected before commit")
        if self.mode == "lost_response" and key not in self.issues:
            self.issues[key] = {"id": "issue-1", **arguments}
            raise UnknownOutcome("response lost after commit")
        if self.mode == "absent" and key not in self.issues:
            raise UnknownOutcome("response lost")
        issue = {"id": f"issue-{self.create_calls}", **arguments}
        self.issues[key] = issue
        return issue

    def inspect(self, arguments: dict[str, Any], key: str) -> dict[str, Any] | None:
        self.inspect_calls += 1
        return self.issues.get(key)

    def inspect_failing(self, arguments: dict[str, Any], key: str) -> dict[str, Any] | None:
        self.inspect_calls += 1
        raise TimeoutError("verification unavailable")


class AcceptanceTests(unittest.TestCase):
    def make_runtime(
        self,
        database: Path,
        service: FakeSideEffect,
        inspector: Any = None,
    ) -> Runtime:
        runtime = Runtime(database)
        runtime.register(
            Tool(
                name="create_issue",
                execute=service.execute,
                inspect=inspector or service.inspect,
            )
        )
        return runtime

    def test_normal_success(self) -> None:
        with TemporaryDirectory() as directory:
            service = FakeSideEffect("success")
            runtime = self.make_runtime(Path(directory) / "actions.db", service)
            result = runtime.execute("create_issue", {"title": "Login"}, idempotency_key="a-1")
            runtime.close()
        self.assertEqual(result.status, "success")
        self.assertEqual(service.create_calls, 1)

    def test_failure_before_commit(self) -> None:
        with TemporaryDirectory() as directory:
            service = FakeSideEffect("before_commit")
            runtime = self.make_runtime(Path(directory) / "actions.db", service)
            result = runtime.execute("create_issue", {"title": "Login"}, idempotency_key="a-1")
            runtime.close()
        self.assertEqual(result.status, "failed")
        self.assertEqual(service.create_calls, 1)

    def test_commit_then_lost_response(self) -> None:
        with TemporaryDirectory() as directory:
            service = FakeSideEffect("lost_response")
            runtime = self.make_runtime(Path(directory) / "actions.db", service)
            initial = runtime.execute("create_issue", {"title": "Login"}, idempotency_key="a-1")
            recovered = runtime.recover(initial.action_id)
            runtime.close()
        self.assertEqual(initial.status, "unknown")
        self.assertEqual(recovered.status, "success")
        self.assertEqual(service.create_calls, 1)

    def test_unknown_with_issue_absent_is_verified_without_inspector_write(self) -> None:
        with TemporaryDirectory() as directory:
            service = FakeSideEffect("absent")
            runtime = self.make_runtime(Path(directory) / "actions.db", service)
            initial = runtime.execute("create_issue", {"title": "Login"}, idempotency_key="a-1")
            recovered = runtime.recover(initial.action_id)
            runtime.close()
        self.assertEqual(recovered.status, "verified_absent")
        self.assertEqual(service.create_calls, 1)
        self.assertEqual(service.inspect_calls, 1)

    def test_verified_absence_then_explicit_retry(self) -> None:
        with TemporaryDirectory() as directory:
            service = FakeSideEffect("absent")
            runtime = self.make_runtime(Path(directory) / "actions.db", service)
            initial = runtime.execute("create_issue", {"title": "Login"}, idempotency_key="a-1")
            runtime.recover(initial.action_id)
            service.mode = "success"
            retried = runtime.retry(initial.action_id)
            runtime.close()
        self.assertEqual(retried.status, "success")
        self.assertEqual(retried.attempt, 2)
        self.assertEqual(service.create_calls, 2)

    def test_inspector_failure_keeps_unknown_without_retry(self) -> None:
        with TemporaryDirectory() as directory:
            service = FakeSideEffect("lost_response")
            runtime = self.make_runtime(
                Path(directory) / "actions.db",
                service,
                service.inspect_failing,
            )
            initial = runtime.execute("create_issue", {"title": "Login"}, idempotency_key="a-1")
            recovered = runtime.recover(initial.action_id)
            runtime.close()
        self.assertEqual(recovered.status, "unknown")
        self.assertEqual(service.create_calls, 1)

    def test_restart_after_unknown(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "actions.db"
            service = FakeSideEffect("lost_response")
            first_runtime = self.make_runtime(database, service)
            initial = first_runtime.execute("create_issue", {"title": "Login"}, idempotency_key="a-1")
            first_runtime.close()

            second_runtime = self.make_runtime(database, service)
            recovered = second_runtime.recover(initial.action_id)
            second_runtime.close()
        self.assertEqual(recovered.status, "success")
        self.assertEqual(service.create_calls, 1)

    def test_same_key_with_different_arguments_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            service = FakeSideEffect("success")
            runtime = self.make_runtime(Path(directory) / "actions.db", service)
            runtime.execute("create_issue", {"title": "Login"}, idempotency_key="a-1")
            with self.assertRaisesRegex(ValueError, "different arguments"):
                runtime.execute("create_issue", {"title": "Billing"}, idempotency_key="a-1")
            runtime.close()


if __name__ == "__main__":
    unittest.main()
