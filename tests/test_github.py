import json
import unittest

import httpx

from agent_recovery import UnknownOutcome
from agent_recovery.integrations.github import GitHubClient


MARKER = "<!-- agent-recovery:idempotency-key=customer-123 -->"


class GitHubClientTests(unittest.TestCase):
    def test_create_issue_sends_marker_and_expected_payload(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                201,
                json={"id": 123, "html_url": "https://github.com/o/r/issues/123"},
                request=request,
            )

        client = GitHubClient(
            "test-token",
            transport=httpx.MockTransport(handler),
        )
        issue = client.create_issue(
            "owner",
            "repo",
            "Login is broken",
            "Customer report",
            "customer-123",
        )
        client.close()

        self.assertEqual(issue["id"], 123)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(requests[0].url.path, "/repos/owner/repo/issues")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["title"], "Login is broken")
        self.assertIn(MARKER, payload["body"])

    def test_find_issue_returns_matching_open_or_closed_issue(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["state"], "all")
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "body": "unrelated"},
                    {"id": 2, "body": f"body\n{MARKER}"},
                ],
                request=request,
            )

        client = GitHubClient("test-token", transport=httpx.MockTransport(handler))
        issue = client.find_issue_by_idempotency_key("owner", "repo", "customer-123")
        client.close()

        self.assertEqual(issue, {"id": 2, "body": f"body\n{MARKER}"})

    def test_create_timeout_becomes_unknown_outcome(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("response lost after dispatch", request=request)

        client = GitHubClient("test-token", transport=httpx.MockTransport(handler))
        with self.assertRaises(UnknownOutcome):
            client.create_issue(
                "owner",
                "repo",
                "Login is broken",
                "Customer report",
                "customer-123",
            )
        client.close()

    def test_create_client_error_remains_a_normal_http_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"message": "Validation Failed"}, request=request)

        client = GitHubClient("test-token", transport=httpx.MockTransport(handler))
        with self.assertRaises(httpx.HTTPStatusError):
            client.create_issue(
                "owner",
                "repo",
                "Login is broken",
                "Customer report",
                "customer-123",
            )
        client.close()

    def test_inspector_timeout_remains_a_verification_exception(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lookup timed out", request=request)

        client = GitHubClient("test-token", transport=httpx.MockTransport(handler))
        with self.assertRaises(httpx.ReadTimeout):
            client.find_issue_by_idempotency_key("owner", "repo", "customer-123")
        client.close()

    def test_find_issue_returns_none_when_marker_is_absent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[{"id": 1, "body": "unrelated"}],
                request=request,
            )

        client = GitHubClient("test-token", transport=httpx.MockTransport(handler))
        issue = client.find_issue_by_idempotency_key("owner", "repo", "customer-123")
        client.close()

        self.assertIsNone(issue)


if __name__ == "__main__":
    unittest.main()
