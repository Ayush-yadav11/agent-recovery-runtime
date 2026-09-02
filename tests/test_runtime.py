from __future__ import annotations

from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import Any
import sqlite3
import unittest

from agent_recovery import Runtime, Tool, UnknownOutcome
from agent_recovery.core.store import ActionStore
from agent_recovery.core.actions import (
    ActionResult as CoreActionResult,
    Tool as CoreTool,
    UnknownOutcome as CoreUnknownOutcome,
)


class FakeIssueService:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.create_calls = 0
        self.fail_before_create = False
        self.fail_after_create = False
        self.inspect_error = False

    def create(self, arguments: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
        self.create_calls += 1
        assert idempotency_key is not None
        if self.fail_before_create:
            raise RuntimeError("request rejected before commit")
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

    def runtime_from_database(self, database: str, service: FakeIssueService) -> Runtime:
        runtime = Runtime(database)
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

    def test_failed_action_cannot_be_reexecuted_with_same_key(self) -> None:
        service = FakeIssueService()
        service.fail_before_create = True
        runtime = self.runtime(service)
        first = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )
        self.assertEqual(first.status, "failed")
        service.issues.clear()
        with self.assertRaisesRegex(ValueError, "new idempotency key"):
            runtime.execute(
                "create_issue",
                {"title": "Login is broken"},
                idempotency_key="customer-123",
            )
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
        runtime.request_retry_approval(absent.action_id)
        runtime.approve_retry(
            absent.action_id,
            reviewer="operator-1",
            reason="Verified absence before retry",
        )
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
        runtime.request_retry_approval(first.action_id)
        runtime.approve_retry(
            first.action_id,
            reviewer="operator-1",
            reason="Verified absence before retry",
        )
        service.fail_after_create = False
        retried = runtime.retry(first.action_id)

        with self.assertRaisesRegex(ValueError, "consumed"):
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

        with self.assertRaisesRegex(ValueError, "retry requires an approved approval"):
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

        with self.assertRaisesRegex(ValueError, "only unknown or running actions can be recovered"):
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
    def test_legacy_action_schema_is_migrated(self) -> None:
        with TemporaryDirectory() as directory:
            database = f"{directory}/legacy.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE actions (
                    action_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (tool_name, idempotency_key)
                );
                CREATE TABLE events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO actions
                    (action_id, tool_name, arguments_json, idempotency_key, status, result_json)
                VALUES
                    ('legacy-1', 'create_issue', '{"title":"Login"}', 'legacy-key', 'success', '{"id":"issue-1"}');
                """
            )
            connection.close()

            service = FakeIssueService()
            runtime = self.runtime_from_database(database, service)
            result = runtime.execute(
                "create_issue",
                {"title": "Login"},
                idempotency_key="legacy-key",
            )
            runtime.close()

        self.assertEqual(result.action_id, "legacy-1")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt, 1)
        self.assertEqual(service.create_calls, 0)

    def test_running_action_can_be_recovered_after_restart(self) -> None:
        with TemporaryDirectory() as directory:
            database = f"{directory}/crashed.db"
            store = ActionStore(database)
            store.create_action(
                action_id="crashed-1",
                tool_name="create_issue",
                arguments={"title": "Login"},
                arguments_hash="hash",
                idempotency_key="crashed-key",
            )
            store.close()

            service = FakeIssueService()
            service.issues["crashed-key"] = {"id": "issue-1", "title": "Login"}
            runtime = self.runtime_from_database(database, service)
            recovered = runtime.recover("crashed-1")
            runtime.close()

        self.assertEqual(recovered.status, "success")
        self.assertEqual(service.create_calls, 0)

    def test_verified_absence_requires_approval_before_retry(self) -> None:
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
        self.assertEqual(absent.status, "verified_absent")

        with self.assertRaises(KeyError):
            runtime.get_retry_approval(absent.action_id)
        with self.assertRaisesRegex(ValueError, "retry requires an approved approval"):
            runtime.retry(absent.action_id)

        approval = runtime.request_retry_approval(absent.action_id)
        pending_again = runtime.request_retry_approval(absent.action_id)

        self.assertEqual(approval.status, "pending")
        self.assertEqual(pending_again.approval_id, approval.approval_id)
        self.assertEqual(service.create_calls, 1)

    def test_pending_approval_does_not_allow_retry(self) -> None:
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
        approval = runtime.request_retry_approval(absent.action_id)
        self.assertEqual(approval.status, "pending")
        service.fail_after_create = False

        with self.assertRaisesRegex(ValueError, "retry requires an approved approval"):
            runtime.retry(absent.action_id)

        still_pending = runtime.get_retry_approval(absent.action_id)
        self.assertEqual(still_pending.status, "pending")
        self.assertIsNone(still_pending.reviewer)
        self.assertEqual(service.create_calls, 1)
        self.assertEqual(service.issues, {})

    def test_rejected_retry_cannot_create_a_side_effect(self) -> None:
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
        runtime.request_retry_approval(absent.action_id)

        rejected = runtime.reject_retry(
            absent.action_id,
            reviewer="operator-1",
            reason="Wait for the external system to catch up",
        )
        self.assertEqual(rejected.status, "rejected")
        with self.assertRaisesRegex(ValueError, "rejected"):
            runtime.retry(absent.action_id)
        self.assertEqual(service.create_calls, 1)

    def test_approved_retry_is_single_use_and_survives_restart(self) -> None:
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
        service.issues.clear()
        absent = runtime.recover(first.action_id)
        approval = runtime.request_retry_approval(absent.action_id)
        runtime.close()

        restarted = Runtime(database)
        restarted.register(ToolFixture(service).tool())
        stored = restarted.get_retry_approval(absent.action_id)
        self.assertEqual(stored.approval_id, approval.approval_id)
        approved = restarted.approve_retry(
            absent.action_id,
            reviewer="operator-1",
            reason="Verified absence in the target repository",
        )
        service.fail_after_create = False
        retried = restarted.retry(absent.action_id)

        self.assertEqual(approved.status, "approved")
        self.assertEqual(retried.status, "success")
        self.assertEqual(retried.attempt, 2)
        self.assertEqual(service.create_calls, 2)
        with self.assertRaisesRegex(ValueError, "consumed"):
            restarted.retry(absent.action_id)
        restarted.close()


if __name__ == "__main__":
    unittest.main()
