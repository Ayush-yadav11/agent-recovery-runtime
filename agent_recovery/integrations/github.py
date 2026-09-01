"""GitHub issue integration with deterministic idempotency markers."""

from __future__ import annotations

import os
from typing import Any, TypeAlias

import httpx

from agent_recovery.core.actions import UnknownOutcome


Transport: TypeAlias = httpx.BaseTransport


def idempotency_marker(key: str) -> str:
    return f"<!-- agent-recovery:idempotency-key={key} -->"


class GitHubClient:
    def __init__(
        self,
        token: str,
        transport: Transport | None = None,
        *,
        base_url: str = "https://api.github.com",
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=transport,
        )

    @classmethod
    def from_env(
        cls,
        *,
        transport: Transport | None = None,
        base_url: str = "https://api.github.com",
    ) -> GitHubClient:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        return cls(token, transport=transport, base_url=base_url)

    def create_issue(
        self,
        owner: str,
        repository: str,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        marker = idempotency_marker(idempotency_key)
        marked_body = body if marker in body else f"{body.rstrip()}\n\n{marker}"
        try:
            response = self._client.post(
                f"/repos/{owner}/{repository}/issues",
                json={"title": title, "body": marked_body},
            )
        except httpx.TransportError as exc:
            raise UnknownOutcome("GitHub create response was lost") from exc

        response.raise_for_status()
        try:
            return _json_object(response)
        except ValueError as exc:
            raise UnknownOutcome("GitHub create response could not be decoded") from exc

    def find_issue_by_idempotency_key(
        self,
        owner: str,
        repository: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        marker = idempotency_marker(idempotency_key)
        page = 1
        while True:
            response = self._client.get(
                f"/repos/{owner}/{repository}/issues",
                params={"state": "all", "per_page": 100, "page": page},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("GitHub issues response must be a list")

            for issue in payload:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                body = issue.get("body") or ""
                if marker in body:
                    return issue

            if len(payload) < 100:
                return None
            page += 1

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _json_object(response: httpx.Response) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("GitHub response must be a JSON object")
    return payload
