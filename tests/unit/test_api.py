"""The webhook service: verify first, act second, ignore most of it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from proofpr import api
from proofpr.ledger import Ledger
from proofpr.settings import Settings
from proofpr.webhooks import signatures

SECRET = "s3cret-webhook-value"


class StubService:
    """What the endpoints need, with the post-run loops stubbed.

    The lifespan is deliberately not run: it builds real adapters and starts a
    scheduler, neither of which is what these tests are about.
    """

    def __init__(self, ledger: Ledger) -> None:
        """Build the stub."""
        self.settings = Settings.model_validate(
            {
                "github": {"webhook_secret": SECRET},
                "linear": {"webhook_secret": SECRET},
            }
        )
        self.ledger = ledger
        self.pull_events: list[dict[str, Any]] = []
        self.notifier = self

    async def on_pull_request_event(self, payload: dict[str, Any]) -> Any:
        """Report a notification for a merged pull request of ours."""
        self.pull_events.append(payload)
        pull = payload.get("pull_request") or {}
        if payload.get("action") == "closed" and pull.get("merged"):
            return type("Notification", (), {"run_id": "r-7f3a"})()
        return None


@pytest.fixture
def service(tmp_path: Path) -> StubService:
    """Install a stub service on the real app."""
    stub = StubService(Ledger(tmp_path / "ledger.db"))
    api.app.state.service = stub
    return stub


@pytest.fixture
def client(service: StubService) -> TestClient:
    """A client that does not run the lifespan."""
    return TestClient(api.app)


def post(client: TestClient, path: str, payload: dict[str, Any], **headers: str) -> Any:
    """Post a signed body, signing exactly the bytes that are sent."""
    body = json.dumps(payload).encode()
    return client.post(path, content=body, headers=headers)


class TestHealth:
    """Liveness and readiness say different things."""

    def test_healthz_is_up(self, client: TestClient) -> None:
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_readyz_reports_the_ledger(self, client: TestClient) -> None:
        response = client.get("/readyz")

        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_readyz_fails_when_the_ledger_is_gone(
        self, client: TestClient, service: StubService
    ) -> None:
        service.ledger.close()

        response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["status"] == "not ready"


class TestGitHubWebhook:
    """A merged pull request of ours closes the loop; everything else does not."""

    def test_a_signed_merge_is_acted_on(self, client: TestClient) -> None:
        payload = {
            "action": "closed",
            "pull_request": {"merged": True, "body": "<!-- proofpr-run:r-7f3a -->"},
        }
        body = json.dumps(payload).encode()

        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": signatures.sign(SECRET, body, provider="github"),
                "X-GitHub-Event": "pull_request",
            },
        )

        assert response.json() == {"status": "notified", "run_id": "r-7f3a"}

    def test_an_unsigned_request_is_rejected_and_never_acted_on(
        self, client: TestClient, service: StubService
    ) -> None:
        response = post(
            client,
            "/webhooks/github",
            {"action": "closed", "pull_request": {"merged": True}},
            **{"X-GitHub-Event": "pull_request"},
        )

        assert response.json()["status"] == "rejected"
        assert service.pull_events == []

    def test_a_forged_signature_is_rejected(self, client: TestClient, service: StubService) -> None:
        response = post(
            client,
            "/webhooks/github",
            {"action": "closed"},
            **{
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
                "X-GitHub-Event": "pull_request",
            },
        )

        assert response.json()["status"] == "rejected"
        assert service.pull_events == []

    def test_a_rejected_request_still_returns_200(self, client: TestClient) -> None:
        # A provider that receives enough 4xx disables the webhook, and a
        # misconfigured secret should not cost the integration.
        response = post(client, "/webhooks/github", {}, **{"X-GitHub-Event": "pull_request"})

        assert response.status_code == 200

    def test_an_event_we_do_not_handle_is_ignored(
        self, client: TestClient, service: StubService
    ) -> None:
        body = json.dumps({"zen": "hello"}).encode()

        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": signatures.sign(SECRET, body, provider="github"),
                "X-GitHub-Event": "ping",
            },
        )

        assert response.json()["status"] == "ignored"
        assert service.pull_events == []

    def test_someone_elses_merged_pull_request_is_ignored(self, client: TestClient) -> None:
        payload = {"action": "closed", "pull_request": {"merged": False, "body": ""}}
        body = json.dumps(payload).encode()

        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": signatures.sign(SECRET, body, provider="github"),
                "X-GitHub-Event": "pull_request",
            },
        )

        assert response.json()["status"] == "ignored"


class TestLinearWebhook:
    """Verified, and honest about doing nothing yet."""

    def test_a_signed_request_is_accepted_and_ignored(self, client: TestClient) -> None:
        body = json.dumps({"action": "update"}).encode()

        response = client.post(
            "/webhooks/linear",
            content=body,
            headers={"Linear-Signature": signatures.sign(SECRET, body, provider="linear")},
        )

        assert response.json()["status"] == "ignored"
        assert "not acted on yet" in response.json()["reason"]

    def test_an_unsigned_request_is_rejected(self, client: TestClient) -> None:
        assert post(client, "/webhooks/linear", {"action": "update"}).json()["status"] == (
            "rejected"
        )
