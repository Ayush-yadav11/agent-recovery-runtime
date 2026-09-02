import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from agent_recovery import Runtime, Tool, UnknownOutcome, VerificationStatus
from agent_recovery.integrations.stripe import StripeClient

KEY = "customer-checkout-123"
INTENT = {"payment_intent_id": "pi_123"}


def response(status_code: int, payload: object = None) -> httpx.Response:
    request = httpx.Request("GET", "https://api.stripe.com/v1/payment_intents")
    if payload is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, json=payload, request=request)


def client_returning(payload: object, status_code: int = 200) -> tuple[StripeClient, Mock]:
    fetch = Mock(return_value=response(status_code, payload))
    return StripeClient(api_key="sk_test_fake", fetch=fetch), fetch


class StripeInspectionTests(unittest.TestCase):
    def test_payment_intent_succeeded_is_found(self) -> None:
        client, fetch = client_returning(
            {"status": "succeeded", "id": "pi_123", "amount": 1000}
        )

        outcome = client.inspect(INTENT, KEY)

        self.assertEqual(outcome.status, VerificationStatus.FOUND)
        self.assertEqual(outcome.value["id"], "pi_123")
        self.assertEqual(outcome.value["amount"], 1000)
        method, url = fetch.call_args.args
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://api.stripe.com/v1/payment_intents/pi_123")

    def test_payment_intent_canceled_is_verified_absent(self) -> None:
        client, _ = client_returning({"status": "canceled", "id": "pi_123"})

        outcome = client.inspect(INTENT, KEY)

        self.assertEqual(outcome.status, VerificationStatus.VERIFIED_ABSENT)

    def test_payment_intent_processing_is_unavailable(self) -> None:
        client, _ = client_returning({"status": "processing", "id": "pi_123"})

        outcome = client.inspect(INTENT, KEY)

        self.assertEqual(outcome.status, VerificationStatus.UNAVAILABLE)
        self.assertIn("processing", outcome.reason)

    def test_missing_payment_intent_returns_verified_absent(self) -> None:
        client, _ = client_returning(None, status_code=404)

        outcome = client.inspect(INTENT, KEY)

        self.assertEqual(outcome.status, VerificationStatus.VERIFIED_ABSENT)

    def test_unknown_payment_intent_id_is_ambiguous_not_absent(self) -> None:
        client, fetch = client_returning({"status": "succeeded", "id": "pi_123"})

        outcome = client.inspect({"amount": 1000}, KEY)

        self.assertEqual(outcome.status, VerificationStatus.AMBIGUOUS)
        fetch.assert_not_called()


class StripeClassificationTests(unittest.TestCase):
    def test_timeout_is_unavailable(self) -> None:
        fetch = Mock(side_effect=httpx.TimeoutException("read timed out"))
        client = StripeClient(api_key="sk_test_fake", fetch=fetch)

        with self.assertRaises(httpx.TimeoutException) as raised:
            client.inspect(INTENT, KEY)

        outcome = client.classify(raised.exception)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, VerificationStatus.UNAVAILABLE)

    def test_connect_error_is_unavailable(self) -> None:
        client, _ = client_returning({"status": "succeeded", "id": "pi_123"})

        outcome = client.classify(httpx.ConnectError("connection refused"))

        self.assertEqual(outcome.status, VerificationStatus.UNAVAILABLE)

    def test_unrecognised_error_is_not_classified(self) -> None:
        client, _ = client_returning({"status": "succeeded", "id": "pi_123"})

        self.assertIsNone(client.classify(ValueError("malformed payload")))


class StripeExecutionTests(unittest.TestCase):
    def test_execute_sends_idempotency_key(self) -> None:
        client, fetch = client_returning(
            {"id": "pi_123", "status": "processing"}, status_code=200
        )

        created = client.execute({"amount": 1000, "currency": "usd"}, KEY)

        self.assertEqual(created["id"], "pi_123")
        method, url = fetch.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.stripe.com/v1/payment_intents")
        headers = fetch.call_args.kwargs["headers"]
        self.assertEqual(headers["Idempotency-Key"], KEY)
        self.assertEqual(headers["Authorization"], "Bearer sk_test_fake")
        self.assertEqual(fetch.call_args.kwargs["data"], {"amount": 1000, "currency": "usd"})

    def test_execute_omits_the_inspection_hint_from_the_request_body(self) -> None:
        client, fetch = client_returning({"id": "pi_123", "status": "processing"})

        client.execute({"amount": 1000, "payment_intent_id": "pi_123"}, KEY)

        self.assertEqual(fetch.call_args.kwargs["data"], {"amount": 1000})

    def test_execute_requires_an_idempotency_key(self) -> None:
        client, fetch = client_returning({"id": "pi_123"})

        with self.assertRaises(ValueError):
            client.execute({"amount": 1000}, None)
        fetch.assert_not_called()

    def test_lost_response_becomes_unknown_outcome(self) -> None:
        fetch = Mock(side_effect=httpx.ReadTimeout("response lost after dispatch"))
        client = StripeClient(api_key="sk_test_fake", fetch=fetch)

        with self.assertRaises(UnknownOutcome):
            client.execute({"amount": 1000}, KEY)

    def test_in_flight_idempotency_conflict_becomes_unknown_outcome(self) -> None:
        client, _ = client_returning({"error": "key in use"}, status_code=409)

        with self.assertRaises(UnknownOutcome):
            client.execute({"amount": 1000}, KEY)

    def test_rejected_request_remains_a_normal_http_failure(self) -> None:
        client, _ = client_returning({"error": "amount must be positive"}, status_code=400)

        with self.assertRaises(httpx.HTTPStatusError):
            client.execute({"amount": -1}, KEY)


class StripeCredentialTests(unittest.TestCase):
    def test_api_key_is_read_from_the_environment(self) -> None:
        fetch = Mock(return_value=response(200, {"id": "pi_123", "status": "processing"}))

        with patch.dict(os.environ, {"STRIPE_API_KEY": "sk_test_from_env"}):
            client = StripeClient(fetch=fetch)
            client.execute({"amount": 1000}, KEY)

        headers = fetch.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk_test_from_env")

    def test_live_client_without_credentials_is_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                StripeClient()

    def test_base_url_is_configurable(self) -> None:
        fetch = Mock(return_value=response(200, {"status": "succeeded", "id": "pi_123"}))
        client = StripeClient(
            api_key="sk_test_fake",
            base_url="https://stripe.test/",
            fetch=fetch,
        )

        client.inspect(INTENT, KEY)

        self.assertEqual(
            fetch.call_args.args[1],
            "https://stripe.test/v1/payment_intents/pi_123",
        )


class StripeRuntimeContractTests(unittest.TestCase):
    """The adapter has to satisfy the runtime's `Tool` contract, not just its own."""

    def register(self, runtime: Runtime, fetch: Mock) -> None:
        client = StripeClient(api_key="sk_test_fake", fetch=fetch)
        runtime.register(
            Tool(
                name=client.name,
                execute=client.execute,
                inspect=client.inspect,
                classify=client.classify,
            )
        )

    def test_lost_create_is_recovered_as_success_when_the_charge_settled(self) -> None:
        fetch = Mock(
            side_effect=[
                httpx.ReadTimeout("response lost after dispatch"),
                response(200, {"id": "pi_123", "status": "succeeded", "amount": 1000}),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            with Runtime(Path(directory) / "stripe.db") as runtime:
                self.register(runtime, fetch)
                started = runtime.execute(
                    "stripe",
                    {"amount": 1000, "payment_intent_id": "pi_123"},
                    idempotency_key=KEY,
                )
                self.assertEqual(started.status, "unknown")

                recovered = runtime.recover(started.action_id)

        self.assertEqual(recovered.status, "success")
        self.assertEqual(recovered.result["id"], "pi_123")

    def test_unsettled_charge_stays_unknown_and_unlocks_no_retry(self) -> None:
        fetch = Mock(
            side_effect=[
                httpx.ReadTimeout("response lost after dispatch"),
                response(200, {"id": "pi_123", "status": "processing"}),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            with Runtime(Path(directory) / "stripe.db") as runtime:
                self.register(runtime, fetch)
                started = runtime.execute(
                    "stripe",
                    {"amount": 1000, "payment_intent_id": "pi_123"},
                    idempotency_key=KEY,
                )

                recovered = runtime.recover(started.action_id)
                self.assertEqual(recovered.status, "unknown")
                # An unsettled charge must not become retryable.
                with self.assertRaises(ValueError):
                    runtime.request_retry_approval(started.action_id)

    def test_unreachable_stripe_stays_unknown_through_the_classifier(self) -> None:
        fetch = Mock(
            side_effect=[
                httpx.ReadTimeout("response lost after dispatch"),
                httpx.ConnectError("connection refused"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            with Runtime(Path(directory) / "stripe.db") as runtime:
                self.register(runtime, fetch)
                started = runtime.execute(
                    "stripe",
                    {"amount": 1000, "payment_intent_id": "pi_123"},
                    idempotency_key=KEY,
                )

                recovered = runtime.recover(started.action_id)

        self.assertEqual(recovered.status, "unknown")
        self.assertIn("unavailable", recovered.error)


if __name__ == "__main__":
    unittest.main()
