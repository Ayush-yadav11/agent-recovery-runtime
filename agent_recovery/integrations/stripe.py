"""Stripe payment intent integration with idempotent execution and inspection.

Stripe has two properties the recovery contract must respect:

1. Creation is retry-safe only through an `Idempotency-Key` header, so the
   runtime's idempotency key is forwarded on every create request.
2. A payment intent settles asynchronously, so a read can legitimately answer
   "not yet" instead of found or absent. Those reads map onto
   `VerificationOutcome.unavailable`, which keeps the action `unknown` rather
   than unlocking a retry that would charge a customer twice.

Inspection retrieves the intent by id, so `arguments` must carry
`payment_intent_id` for verification to be possible. Stripe generates that id
server-side, so a create call whose response was lost leaves nothing to read
back; recovering that case needs the id from a webhook or an out-of-band store.
Without the id this adapter reports `ambiguous`, never `verified_absent`.
"""

from __future__ import annotations

import os
from typing import Any, Callable, TypeAlias

import httpx

from agent_recovery.core.actions import UnknownOutcome, VerificationOutcome

FetchFn: TypeAlias = Callable[..., httpx.Response]

DEFAULT_BASE_URL = "https://api.stripe.com"
PAYMENT_INTENTS_PATH = "/v1/payment_intents"

_SUCCEEDED = "succeeded"
_ABSENT_STATUSES = frozenset({"canceled", "requires_refund"})
_INSPECTION_ID = "payment_intent_id"


class StripeClient:
    """A Stripe payment intent tool: create it, then read back its status."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        fetch: FetchFn | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("STRIPE_API_KEY")
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        if fetch is None and not self._api_key:
            raise ValueError("STRIPE_API_KEY is required")
        self._fetch: FetchFn = fetch or _http_fetch

    @property
    def name(self) -> str:
        return "stripe"

    def execute(
        self,
        arguments: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Create a payment intent, replayable under the same idempotency key."""
        if not idempotency_key:
            raise ValueError("idempotency_key is required to create a payment intent")

        headers = self._headers()
        headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._fetch(
                "POST",
                f"{self._base_url}{PAYMENT_INTENTS_PATH}",
                headers=headers,
                data=_form_params(arguments),
            )
        except httpx.TransportError as exc:
            raise UnknownOutcome("Stripe payment intent response was lost") from exc

        # A conflict means a request with this key is still in flight, and a
        # server error can follow a committed charge. Neither says anything
        # about the customer's money, so both stay unknown.
        if response.status_code == 409 or response.status_code >= 500:
            raise UnknownOutcome(
                f"Stripe payment intent is unconfirmed (HTTP {response.status_code})"
            )
        response.raise_for_status()
        try:
            return _json_object(response)
        except ValueError as exc:
            raise UnknownOutcome("Stripe payment intent response could not be decoded") from exc

    def inspect(
        self,
        arguments: dict[str, Any],
        idempotency_key: str | None,
    ) -> VerificationOutcome:
        """Read one payment intent and translate its status into a verdict."""
        payment_intent_id = str(arguments.get(_INSPECTION_ID) or "").strip()
        if not payment_intent_id:
            return VerificationOutcome.ambiguous(
                f"{_INSPECTION_ID} is missing, so the payment intent cannot be read back"
            )

        response = self._fetch(
            "GET",
            f"{self._base_url}{PAYMENT_INTENTS_PATH}/{payment_intent_id}",
            headers=self._headers(),
        )
        if response.status_code == 404:
            return VerificationOutcome.verified_absent(
                f"Stripe holds no payment intent {payment_intent_id}"
            )
        response.raise_for_status()

        payload = _json_object(response)
        status = payload.get("status")
        if status == _SUCCEEDED:
            return VerificationOutcome.found(payload)
        if status in _ABSENT_STATUSES:
            return VerificationOutcome.verified_absent(
                f"payment intent {payment_intent_id} is {status}"
            )
        if not status:
            return VerificationOutcome.ambiguous(
                f"Stripe returned payment intent {payment_intent_id} without a status"
            )
        # Every remaining state is in flight. The charge is neither confirmed
        # nor ruled out, so the verdict is deferred instead of guessed.
        return VerificationOutcome.unavailable(
            f"payment intent {payment_intent_id} is {status} and has not settled"
        )

    def classify(self, exc: BaseException) -> VerificationOutcome | None:
        """Defer the verdict when Stripe could not be reached at all."""
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            return VerificationOutcome.unavailable(
                f"Stripe could not be reached: {type(exc).__name__}"
            )
        return None

    def _headers(self) -> dict[str, str]:
        headers = {"Stripe-Version": "2024-06-20"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


def _http_fetch(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: dict[str, Any] | None = None,
) -> httpx.Response:
    """The default transport: one form-encoded Stripe request."""
    return httpx.request(method, url, headers=headers, data=data)


def _form_params(arguments: dict[str, Any]) -> dict[str, Any]:
    """Stripe takes flat form parameters, not JSON.

    `payment_intent_id` is an inspection hint carried alongside the create
    arguments, so it is not sent to Stripe. Nested parameters would need
    Stripe's bracket syntax and are out of scope here.
    """
    return {
        key: value
        for key, value in arguments.items()
        if key != _INSPECTION_ID and value is not None
    }


def _json_object(response: httpx.Response) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Stripe response must be a JSON object")
    return payload
