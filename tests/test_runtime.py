from __future__ import annotations

import sqlite3
import unittest
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import Any, Callable

from agent_recovery import Runtime, Tool, UnknownOutcome, VerificationOutcome
from agent_recovery.core.actions import (
    ActionResult as CoreActionResult,
)
from agent_recovery.core.actions import (
    Tool as CoreTool,
)
from agent_recovery.core.actions import (
    UnknownOutcome as CoreUnknownOutcome,
)
from agent_recovery.core.actions import (
    VerificationOutcome as CoreVerificationOutcome,
)
from agent_recovery.core.actions import (
    VerificationStatus,
)
from agent_recovery.core.store import ActionStore


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
                    ('legacy-1', 'create_issue', '{"title":"Login"}', 'legacy-key', 'success',
                     '{"id":"issue-1"}');
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


class TransientLookupError(RuntimeError):
    """The inspector could not reach the external system."""


class InconclusiveLookupError(RuntimeError):
    """The inspector read conflicting records."""


InspectStub = Callable[[dict[str, Any], str | None], Any]
ClassifyStub = Callable[[BaseException], Any]


class LostResponseService:
    """A side effect that commits and then loses its response."""

    def __init__(self, inspect: InspectStub, classify: ClassifyStub | None = None) -> None:
        self.create_calls = 0
        self.inspect_calls = 0
        self._inspect = inspect
        self._classify = classify

    def execute(self, arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
        self.create_calls += 1
        raise UnknownOutcome("response lost after the side effect was committed")

    def inspect(self, arguments: dict[str, Any], key: str | None) -> Any:
        self.inspect_calls += 1
        return self._inspect(arguments, key)

    def tool(self) -> Tool:
        return Tool(
            name="create_issue",
            execute=self.execute,
            inspect=self.inspect,
            classify=self._classify,
        )


class VerificationOutcomeTests(unittest.TestCase):
    """Inspection must distinguish absence from an unusable external system."""

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.database = f"{self._tempdir.name}/runs.db"

    def unknown_action(
        self,
        inspect: InspectStub,
        classify: ClassifyStub | None = None,
    ) -> tuple[Runtime, LostResponseService, Any]:
        service = LostResponseService(inspect, classify)
        runtime = Runtime(self.database)
        self.addCleanup(runtime.close)
        runtime.register(service.tool())
        action = runtime.execute(
            "create_issue",
            {"title": "Login is broken"},
            idempotency_key="customer-123",
        )
        self.assertEqual(action.status, "unknown")
        return runtime, service, action

    def event_types(self, action_id: str) -> list[str]:
        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT event_type FROM events WHERE action_id = ? ORDER BY event_id",
                (action_id,),
            ).fetchall()
        finally:
            connection.close()
        return [row[0] for row in rows]

    def assert_retry_is_blocked(self, runtime: Runtime, action_id: str) -> None:
        with self.assertRaisesRegex(ValueError, "only verified_absent actions can be retried"):
            runtime.request_retry_approval(action_id)
        with self.assertRaisesRegex(ValueError, "retry requires an approved approval"):
            runtime.retry(action_id)

    def test_unavailable_inspection_keeps_action_unknown(self) -> None:
        runtime, service, action = self.unknown_action(
            lambda arguments, key: VerificationOutcome.unavailable("issue search returned 503")
        )

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "unknown")
        self.assertIn("unavailable", recovered.error or "")
        self.assertIn("issue search returned 503", recovered.error or "")
        self.assert_retry_is_blocked(runtime, action.action_id)
        self.assertEqual(service.create_calls, 1)
        self.assertEqual(service.inspect_calls, 1)
        self.assertIn("verification.unavailable", self.event_types(action.action_id))

    def test_ambiguous_inspection_keeps_action_unknown_for_human_review(self) -> None:
        runtime, service, action = self.unknown_action(
            lambda arguments, key: VerificationOutcome.ambiguous(
                "two issues carry the same idempotency marker"
            )
        )

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "unknown")
        self.assertIn("ambiguous", recovered.error or "")
        self.assertIn("two issues carry the same idempotency marker", recovered.error or "")
        self.assert_retry_is_blocked(runtime, action.action_id)
        self.assertEqual(service.create_calls, 1)
        self.assertIn("verification.ambiguous", self.event_types(action.action_id))

    def test_classify_transient_error_as_unavailable(self) -> None:
        def inspect(arguments: dict[str, Any], key: str | None) -> Any:
            raise TransientLookupError("issue search timed out")

        def classify(exc: BaseException) -> VerificationOutcome | None:
            if isinstance(exc, TransientLookupError):
                return VerificationOutcome.unavailable(
                    "the issue index is temporarily unreachable"
                )
            return None

        runtime, service, action = self.unknown_action(inspect, classify)

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "unknown")
        self.assertIn("unavailable", recovered.error or "")
        self.assertIn("the issue index is temporarily unreachable", recovered.error or "")
        self.assertIn("TransientLookupError: issue search timed out", recovered.error or "")
        self.assert_retry_is_blocked(runtime, action.action_id)
        self.assertEqual(service.create_calls, 1)
        self.assertIn("verification.unavailable", self.event_types(action.action_id))

    def test_existing_none_result_still_verified_absent(self) -> None:
        runtime, service, action = self.unknown_action(lambda arguments, key: None)

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "verified_absent")
        self.assertEqual(
            recovered.error,
            "verification did not find the expected side effect",
        )
        self.assertEqual(service.create_calls, 1)
        self.assertIn("verification.verified_absent", self.event_types(action.action_id))
        approval = runtime.request_retry_approval(action.action_id)
        self.assertEqual(approval.status, "pending")

    def test_existing_value_result_still_success(self) -> None:
        issue = {"id": "issue-1", "title": "Login is broken"}
        runtime, service, action = self.unknown_action(lambda arguments, key: issue)

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "success")
        self.assertEqual(recovered.result, issue)
        self.assertIsNone(recovered.error)
        self.assertEqual(service.create_calls, 1)
        self.assertIn("verification.found", self.event_types(action.action_id))

    def test_found_outcome_marks_action_success(self) -> None:
        issue = {"id": "issue-1", "title": "Login is broken"}
        runtime, service, action = self.unknown_action(
            lambda arguments, key: VerificationOutcome.found(issue)
        )

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "success")
        self.assertEqual(recovered.result, issue)
        self.assertEqual(service.create_calls, 1)

    def test_unclassified_inspection_error_keeps_legacy_unknown_behavior(self) -> None:
        def inspect(arguments: dict[str, Any], key: str | None) -> Any:
            raise InconclusiveLookupError("issue lookup unavailable")

        def classify(exc: BaseException) -> VerificationOutcome | None:
            return None

        runtime, service, action = self.unknown_action(inspect, classify)

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "unknown")
        self.assertEqual(
            recovered.error,
            "verification failed: InconclusiveLookupError: issue lookup unavailable",
        )
        self.assert_retry_is_blocked(runtime, action.action_id)
        self.assertIn("verification.error", self.event_types(action.action_id))

    def test_classifier_cannot_promote_an_exception_to_verified_absent(self) -> None:
        def inspect(arguments: dict[str, Any], key: str | None) -> Any:
            raise TransientLookupError("issue search timed out")

        runtime, service, action = self.unknown_action(
            inspect,
            lambda exc: VerificationOutcome.verified_absent("assume nothing was created"),
        )

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "unknown")
        self.assertIn("verification failed", recovered.error or "")
        self.assert_retry_is_blocked(runtime, action.action_id)
        self.assertIn("verification.error", self.event_types(action.action_id))

    def test_broken_classifier_keeps_action_unknown(self) -> None:
        def inspect(arguments: dict[str, Any], key: str | None) -> Any:
            raise TransientLookupError("issue search timed out")

        def classify(exc: BaseException) -> VerificationOutcome | None:
            raise ValueError("classifier is misconfigured")

        runtime, service, action = self.unknown_action(inspect, classify)

        recovered = runtime.recover(action.action_id)

        self.assertEqual(recovered.status, "unknown")
        self.assertIn("verification failed", recovered.error or "")
        self.assert_retry_is_blocked(runtime, action.action_id)

    def test_unavailable_inspection_can_be_verified_after_the_system_recovers(self) -> None:
        issue = {"id": "issue-1", "title": "Login is broken"}
        outcomes = [
            VerificationOutcome.unavailable("issue search returned 503"),
            VerificationOutcome.found(issue),
        ]
        runtime, service, action = self.unknown_action(
            lambda arguments, key: outcomes.pop(0)
        )

        deferred = runtime.recover(action.action_id)
        recovered = runtime.recover(action.action_id)

        self.assertEqual(deferred.status, "unknown")
        self.assertEqual(recovered.status, "success")
        self.assertEqual(recovered.result, issue)
        self.assertEqual(service.create_calls, 1)
        self.assertEqual(service.inspect_calls, 2)

    def test_outcome_contract_rejects_unusable_values(self) -> None:
        self.assertIs(VerificationOutcome, CoreVerificationOutcome)
        self.assertEqual(
            VerificationOutcome.found({"id": 1}).status,
            VerificationStatus.FOUND,
        )
        self.assertEqual(
            VerificationOutcome.verified_absent().status,
            VerificationStatus.VERIFIED_ABSENT,
        )
        with self.assertRaisesRegex(ValueError, "found outcome requires"):
            VerificationOutcome.found(None)
        with self.assertRaisesRegex(ValueError, "requires a reason"):
            VerificationOutcome.unavailable("")
        with self.assertRaisesRegex(ValueError, "requires a reason"):
            VerificationOutcome.ambiguous("   ")


if __name__ == "__main__":
    unittest.main()
