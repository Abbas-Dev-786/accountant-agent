import unittest
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, configure_auth_verifier, configure_workflow_store, service
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

    def bootstrap_organization(self, **kwargs):
        organization = OrganizationSummary(kwargs["organization_id"], kwargs["organization_name"], "controller")
        self.organizations = (organization,)
        return organization

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


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        service.runs.clear()
        self.store = FakeWorkflowStore()
        configure_auth_verifier(FakeVerifier())
        configure_workflow_store(self.store)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        configure_auth_verifier(None)
        configure_workflow_store(None)

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

    def test_bootstrap_is_limited_to_the_configured_controller_and_one_us_organization(self) -> None:
        self.store.organizations = ()
        with patch.dict("os.environ", {"ACCOUNTINGOS_BOOTSTRAP_CONTROLLER_EMAIL": "controller@example.test"}):
            allowed = self.client.post(
                "/api/v1/organizations/bootstrap",
                json={"organization_id": "acme-us", "name": "Acme US"},
                headers=self._headers(),
            )
            rejected = self.client.post(
                "/api/v1/organizations/bootstrap",
                json={"organization_id": "another-us", "name": "Another organization"},
                headers=self._headers(),
            )
        self.assertEqual(allowed.status_code, 201)
        self.assertEqual(rejected.status_code, 409)


if __name__ == "__main__":
    unittest.main()
