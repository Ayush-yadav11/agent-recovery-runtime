import unittest

from agent_recovery.langgraph.state import RecoveryState
from agent_recovery.langgraph.workflow import route_after_execute, route_after_verify


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


if __name__ == "__main__":
    unittest.main()
