import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import (
    app, configure_auth_verifier, configure_plaid_webhook_verifier,
    configure_workflow_store, service, stream_close_run_events,
)
from app.supabase_auth import AuthenticationError, SupabaseUser
from app.supabase_db import OrganizationSummary, PersistedCloseRun, PersistedTask, PersistedTaskEvent


class FakeVerifier:
    def authenticate(self, token):
        if token != "test-token":
            raise AuthenticationError("invalid token")
        return SupabaseUser("controller-1", "controller@example.test", "https://demo.supabase.co/auth/v1")


class FakeWorkflowStore:
    def __init__(self):
        self.runs = {}
        self.keys = {}
        self.organizations = (OrganizationSummary("us-org", "US organization", "controller"),)

    def membership_role(self, organization_id, issuer, subject):
        return "controller" if organization_id == "us-org" and subject == "controller-1" else None

    def organizations_for_user(self, issuer, subject):
        return self.organizations

    def create_close_run(self, **kwargs):
        key = (kwargs["organization_id"], kwargs["idempotency_key"])
        if key not in self.keys:
            run_id = str(uuid4())
            self.keys[key] = PersistedCloseRun(
                run_id,
                kwargs["organization_id"],
                kwargs["period_start"],
                kwargs["period_end"],
                "synchronizing",
                kwargs["deployment"].mode,
                kwargs["deployment"].data_class,
                None,
                None,
            )
            self.runs[run_id] = self.keys[key]
        return self.keys[key]

    def get_close_run(self, run_id):
        return self.runs.get(run_id)

    def close_runs_for_organization(self, organization_id, *, limit=50):
        return tuple(run for run in self.runs.values() if run.organization_id == organization_id)[:limit]

    def connections_for_organization(self, organization_id):
        return ()

    def tasks_for_run(self, run_id):
        return (
            PersistedTask("task-1", run_id, "preflight", "ready", 0, None, None, None),
            PersistedTask("task-2", run_id, "synchronize_sources", "pending", 0, None, None, None, ("preflight",)),
        )

    def events_for_run(self, run_id, **kwargs):
        return (
            PersistedTaskEvent(
                1,
                "us-org",
                run_id,
                "task-1",
                "run_created",
                {"task_count": 5},
                datetime(2026, 7, 1, tzinfo=timezone.utc),
            ),
        )

    def retry_run(self, run_id):
        return self.runs[run_id]

    def cancel_run(self, run_id):
        return self.runs[run_id]

    def review_data_for_run(self, run_id):
        return SimpleNamespace(
            mapping=SimpleNamespace(configuration={"permitted_journal_account_codes": ["200"]}),
            actions=(),
        )

    def create_review_package(self, **kwargs):
        self.review_proposals = kwargs["proposals"]
        return SimpleNamespace(package_hash="package-hash", status="review_frozen")

    def record_webhook_receipt(self, **kwargs):
        self.webhook_receipts = getattr(self, "webhook_receipts", []) + [kwargs]
        return len(self.webhook_receipts) == 1


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        service.runs.clear()
        self.store = FakeWorkflowStore()
        configure_auth_verifier(FakeVerifier())
        configure_workflow_store(self.store)
        configure_plaid_webhook_verifier(None)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        configure_auth_verifier(None)
        configure_workflow_store(None)
        configure_plaid_webhook_verifier(None)

    @staticmethod
    def _headers():
        return {"Authorization": "Bearer test-token"}

    def test_health_discloses_the_live_us_boundary(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["mode"], "production")
        self.assertEqual(response.json()["data_class"], "live")
        self.assertEqual(response.json()["market"], "US")

    def test_new_run_starts_in_synchronizing_state(self) -> None:
        response = self.client.post(
            "/api/v1/close-runs",
            json={"organization_id": "us-org", "period_start": "2026-07-01", "period_end": "2026-07-31"},
            headers={**self._headers(), "Idempotency-Key": "create-july"},
        )
        self.assertEqual(response.status_code, 201)
        run = self.client.get(f"/api/v1/close-runs/{response.json()['id']}", headers=self._headers())
        self.assertEqual(run.status_code, 200)
        self.assertEqual(run.json()["status"], "synchronizing")
        self.assertEqual(run.json()["deployment"], {"mode": "production", "data_class": "live"})

    def test_connections_are_scoped_to_the_requested_organization(self) -> None:
        response = self.client.get("/api/v1/organizations/us-org/connections", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_close_runs_can_be_listed_after_a_page_reload(self) -> None:
        created = self.client.post(
            "/api/v1/close-runs",
            json={"organization_id": "us-org", "period_start": "2026-07-01", "period_end": "2026-07-31"},
            headers={**self._headers(), "Idempotency-Key": "listed-july"},
        ).json()
        listed = self.client.get("/api/v1/organizations/us-org/close-runs", headers=self._headers())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([run["id"] for run in listed.json()], [created["id"]])

    def test_workflow_calls_require_a_bearer_token(self) -> None:
        response = self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, 401)

    def test_invalid_bearer_token_is_rejected(self) -> None:
        response = self.client.get("/api/v1/me", headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(response.status_code, 401)

    def test_close_run_requires_an_idempotency_key(self) -> None:
        response = self.client.post(
            "/api/v1/close-runs",
            json={"organization_id": "us-org", "period_start": "2026-07-01", "period_end": "2026-07-31"},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_task_and_event_timeline_are_organization_scoped(self) -> None:
        created = self.client.post(
            "/api/v1/close-runs",
            json={"organization_id": "us-org", "period_start": "2026-07-01", "period_end": "2026-07-31"},
            headers={**self._headers(), "Idempotency-Key": "timeline-july"},
        ).json()
        tasks = self.client.get(f"/api/v1/close-runs/{created['id']}/tasks", headers=self._headers())
        events = self.client.get(f"/api/v1/close-runs/{created['id']}/events", headers=self._headers())
        self.assertEqual(tasks.status_code, 200)
        self.assertEqual(tasks.json()[1]["dependencies"], ["preflight"])
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()[0]["type"], "run_created")

    def test_prepare_review_rejects_unpermitted_journal_account_codes(self) -> None:
        created = self.client.post(
            "/api/v1/close-runs",
            json={"organization_id": "us-org", "period_start": "2026-07-01", "period_end": "2026-07-31"},
            headers={**self._headers(), "Idempotency-Key": "prepare-review-july"},
        ).json()
        response = self.client.post(
            f"/api/v1/close-runs/{created['id']}/prepare-review",
            json=[{
                "proposal_id": "proposal-1", "journal_date": "2026-07-31", "narration": "Unapproved account",
                "lines": [
                    {"account_code": "999", "debit": "1", "credit": "0", "evidence_ids": ["evidence-1"]},
                    {"account_code": "999", "debit": "0", "credit": "1", "evidence_ids": ["evidence-1"]},
                ],
            }],
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("invalid account code", response.json()["detail"])

    def test_plaid_webhook_is_verified_and_deduplicated(self) -> None:
        body = b'{"request_id":"evt-1","webhook_type":"TRANSACTIONS","webhook_code":"SYNC_UPDATES_AVAILABLE"}'
        with patch("app.main.PlaidLinkConfig.from_environment"), patch(
            "app.main.secret_store_from_environment"
        ), patch("app.main.PlaidWebhookVerifier.verify"):
            first = self.client.post(
                "/api/v1/webhooks/plaid",
                content=body,
                headers={"Plaid-Verification": "verified.jwt.signature"},
            )
            duplicate = self.client.post(
                "/api/v1/webhooks/plaid",
                content=body,
                headers={"Plaid-Verification": "verified.jwt.signature"},
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json(), {"accepted": True})
        self.assertEqual(duplicate.status_code, 202)
        self.assertEqual(duplicate.json(), {"accepted": False})

    def test_plaid_webhook_body_is_bounded_before_json_parsing(self) -> None:
        response = self.client.post(
            "/api/v1/webhooks/plaid",
            content=b"x" * (1_048_576 + 1),
            headers={"Plaid-Verification": "present"},
        )
        self.assertEqual(response.status_code, 413)

    def test_sse_replays_events_committed_just_before_terminal_close(self) -> None:
        run_id = "run-stream"
        terminal_run = PersistedCloseRun(
            run_id, "us-org", "2026-07-01", "2026-07-31", "awaiting_approval", "production", "live", None, None,
        )
        final_event = PersistedTaskEvent(
            2, "us-org", run_id, "task-1", "task_completed", {}, datetime(2026, 7, 1, tzinfo=timezone.utc),
        )

        class StreamStore(FakeWorkflowStore):
            def __init__(self):
                super().__init__()
                self.runs[run_id] = terminal_run
                self.event_reads = 0

            def events_for_run(self, _run_id, **kwargs):
                self.event_reads += 1
                return (final_event,) if self.event_reads == 2 else ()

        stream_store = StreamStore()
        configure_workflow_store(stream_store)

        async def read_stream():
            response = await stream_close_run_events(
                run_id, SupabaseUser("controller-1", "controller@example.test", "https://demo.supabase.co/auth/v1"),
            )
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(read_stream())
        self.assertTrue(any("id: 2" in chunk for chunk in chunks))

if __name__ == "__main__":
    unittest.main()
