from __future__ import annotations

from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import Any
import unittest

from agent_recovery import Runtime, Tool, UnknownOutcome
from agent_recovery.core.actions import (
    ActionResult as CoreActionResult,
    Tool as CoreTool,
    UnknownOutcome as CoreUnknownOutcome,
)


class FakeIssueService:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.create_calls = 0
        self.fail_after_create = False
        self.inspect_error = False

    def create(self, arguments: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
        self.create_calls += 1
        assert idempotency_key is not None
        issue = {
            "id": f"issue-{len(self.issues) + 1}",
            "title": arguments["title"],
            "idempotency_key": idempotency_key,
        }
        self.issues[idempotency_key] = issue
        if self.fail_after_create:
            raise UnknownOutcome("request timed out after the issue was created")
        return issue

    def find(self, arguments: dict[str, Any], idempotency_key: str | None) -> dict[str, Any] | None:
        if self.inspect_error:
            raise RuntimeError("issue lookup unavailable")
        assert idempotency_key is not None
        return self.issues.get(idempotency_key)


@dataclass
class ToolFixture:
    service: FakeIssueService

    def tool(self) -> Tool:
        return Tool(
            name="create_issue",
            execute=self.service.create,
            inspect=self.service.find,
        )


class RuntimeTests(unittest.TestCase):
    def runtime(self, service: FakeIssueService) -> Runtime:
        self.tempdir = TemporaryDirectory()
        runtime = Runtime(f"{self.tempdir.name}/runs.db")
        runtime.register(ToolFixture(service).tool())
        return runtime

    def tearDown(self) -> None:
        if hasattr(self, "tempdir"):
            self.tempdir.cleanup()

    def test_public_action_types_are_owned_by_core_module(self) -> None:
        self.assertIs(Tool, CoreTool)
        self.assertIs(UnknownOutcome, CoreUnknownOutcome)
        self.assertEqual(CoreActionResult.__module__, "agent_recovery.core.actions")

    def test_successful_idempotent_action_is_not_executed_twice(self) -> None:
        service = FakeIssueService()
        runtime = self.runtime(service)

        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )
        second = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )

        self.assertEqual(first.status, "success")
        self.assertEqual(second.action_id, first.action_id)
        self.assertEqual(service.create_calls, 1)

    def test_same_idempotency_key_with_different_arguments_is_rejected(self) -> None:
        service = FakeIssueService()
        runtime = self.runtime(service)
        runtime.execute(
            "create_issue",
            {"title": "Original"},
            idempotency_key="customer-123",
        )

        with self.assertRaisesRegex(ValueError, "different arguments"):
            runtime.execute(
                "create_issue",
                {"title": "Changed"},
                idempotency_key="customer-123",
            )

    def test_unknown_outcome_is_verified_without_repeating_side_effect(self) -> None:
        service = FakeIssueService()
        service.fail_after_create = True
        runtime = self.runtime(service)

        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )

        self.assertEqual(first.status, "unknown")
        self.assertEqual(service.create_calls, 1)

        recovered = runtime.recover(first.action_id)

        self.assertEqual(recovered.status, "success")
        self.assertEqual(
            recovered.result,
            {
                "id": "issue-1",
                "title": "Login is broken",
                "idempotency_key": "customer-123",
            },
        )
        self.assertEqual(service.create_calls, 1)

    def test_unknown_outcome_without_verified_side_effect_stays_failed(self) -> None:
        service = FakeIssueService()
        service.fail_after_create = True
        runtime = self.runtime(service)

        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )
        service.issues.clear()

        recovered = runtime.recover(first.action_id)

        self.assertEqual(recovered.status, "verified_absent")
        self.assertEqual(
            recovered.error,
            "verification did not find the expected side effect",
        )
        self.assertEqual(service.create_calls, 1)

    def test_verified_absence_allows_an_explicit_retry(self) -> None:
        service = FakeIssueService()
        service.fail_after_create = True
        runtime = self.runtime(service)

        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )
        service.issues.clear()
        absent = runtime.recover(first.action_id)
        service.fail_after_create = False

        retried = runtime.retry(first.action_id)

        self.assertEqual(absent.status, "verified_absent")
        self.assertEqual(retried.status, "success")
        self.assertEqual(retried.attempt, 2)
        self.assertEqual(service.create_calls, 2)

    def test_verified_absence_requires_explicit_retry(self) -> None:
        service = FakeIssueService()
        service.fail_after_create = True
        runtime = self.runtime(service)
        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )
        service.issues.clear()
        runtime.recover(first.action_id)

        with self.assertRaisesRegex(ValueError, "use retry"):
            runtime.execute(
                "create_issue",
                {"title": "Login is broken"},
                idempotency_key="customer-123",
            )

    def test_only_the_latest_verified_absence_can_be_retried(self) -> None:
        service = FakeIssueService()
        service.fail_after_create = True
        runtime = self.runtime(service)
        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )
        service.issues.clear()
        runtime.recover(first.action_id)
        service.fail_after_create = False
        retried = runtime.retry(first.action_id)

        with self.assertRaisesRegex(ValueError, "latest"):
            runtime.retry(first.action_id)
        self.assertEqual(retried.attempt, 2)

    def test_unknown_action_is_not_retryable_before_verification(self) -> None:
        service = FakeIssueService()
        service.fail_after_create = True
        runtime = self.runtime(service)
        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )

        with self.assertRaisesRegex(ValueError, "only verified_absent actions can be retried"):
            runtime.retry(first.action_id)

    def test_inspector_failure_keeps_action_unknown(self) -> None:
        service = FakeIssueService()
        service.fail_after_create = True
        service.inspect_error = True
        runtime = self.runtime(service)
        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )

        recovered = runtime.recover(first.action_id)

        self.assertEqual(recovered.status, "unknown")
        self.assertIn("verification failed", recovered.error or "")
        self.assertEqual(service.create_calls, 1)

    def test_recovering_a_non_unknown_action_is_rejected(self) -> None:
        service = FakeIssueService()
        runtime = self.runtime(service)
        result = runtime.execute("create_issue", {"title": "A"}, idempotency_key="a")

        with self.assertRaisesRegex(ValueError, "only unknown actions can be recovered"):
            runtime.recover(result.action_id)

    def test_recovery_state_survives_runtime_restart(self) -> None:
        service = FakeIssueService()
        service.fail_after_create = True
        self.tempdir = TemporaryDirectory()
        database = f"{self.tempdir.name}/runs.db"
        runtime = Runtime(database)
        runtime.register(ToolFixture(service).tool())

        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )
        runtime.close()

        restarted = Runtime(database)
        restarted.register(ToolFixture(service).tool())
        recovered = restarted.recover(first.action_id)
        restarted.close()

        self.assertEqual(recovered.status, "success")
        self.assertEqual(service.create_calls, 1)

    def test_unknown_outcome_requires_an_idempotency_key(self) -> None:
        service = FakeIssueService()
        runtime = self.runtime(service)

        with self.assertRaisesRegex(ValueError, "idempotency_key"):
            runtime.execute("create_issue", {"title": "A"})


if __name__ == "__main__":
    unittest.main()
