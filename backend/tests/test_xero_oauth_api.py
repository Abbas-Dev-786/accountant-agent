import unittest
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.main import app, configure_auth_verifier, configure_workflow_store, configure_xero_oauth, xero_oauth_sessions
from app.supabase_auth import SupabaseUser
from app.supabase_db import OrganizationSummary
from app.xero_oauth import FormResponse, TenantResponse, XeroOAuthClient, XeroOAuthConfig


class FakeSecrets:
    def __init__(self):
        self.values = {"secret://client": "client-secret", "secret://refresh": "old-refresh"}

    def resolve(self, ref):
        return self.values[ref]

    def store(self, ref, value):
        self.values[ref] = value


class FakeTransport:
    def post(self, url, headers, form):
        return FormResponse(200, {"access_token": "access", "refresh_token": "rotated", "expires_in": 1800, "token_type": "Bearer"}, {})


class FakeTenantTransport:
    def get(self, url, headers):
        return TenantResponse(200, [{"id": "connection-1", "tenantId": "tenant-1", "tenantType": "ORGANISATION"}])


def client():
    config = XeroOAuthConfig(
        "client-id",
        "secret://client",
        "secret://refresh",
        "http://localhost:8000/api/v1/connections/xero/callback",
        ("offline_access", "accounting.settings.read"),
    )
    return XeroOAuthClient(config, FakeSecrets(), FakeTransport(), FakeTenantTransport())


class FakeVerifier:
    def authenticate(self, token):
        return SupabaseUser("controller-1", "controller@example.test", "https://demo.supabase.co/auth/v1")


class FakeWorkflowStore:
    def __init__(self):
        self.registered_connections = []

    def membership_role(self, organization_id, issuer, subject):
        return "controller" if organization_id == "demo-org" else None

    def organizations_for_user(self, issuer, subject):
        return (OrganizationSummary("demo-org", "Demo organization", "controller"),)

    def connections_for_organization(self, organization_id):
        return ()

    def upsert_connection(self, **kwargs):
        self.registered_connections.append(kwargs["connection_health"])


class XeroOAuthApiTests(unittest.TestCase):
    def tearDown(self):
        configure_xero_oauth(None)
        configure_auth_verifier(None)
        configure_workflow_store(None)
        xero_oauth_sessions._sessions.clear()

    def test_authorize_and_callback_exchange_without_returning_tokens(self):
        configure_xero_oauth(client())
        configure_auth_verifier(FakeVerifier())
        configure_workflow_store(FakeWorkflowStore())
        http = TestClient(app)
        response = http.get("/api/v1/organizations/demo-org/connections/xero/authorize", headers={"Authorization": "Bearer token"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        query = parse_qs(urlsplit(payload["authorization_url"]).query)
        callback = http.get("/api/v1/connections/xero/callback", params={"state": payload["state"], "code": "one-time-code"})
        self.assertEqual(callback.status_code, 200)
        self.assertEqual(callback.json(), {"status": "authorized", "organization_id": "demo-org", "expires_in": 1800})
        self.assertNotIn("access", callback.text)
        self.assertEqual(query["code_challenge_method"], ["S256"])

    def test_callback_state_is_single_use(self):
        configure_xero_oauth(client())
        configure_auth_verifier(FakeVerifier())
        configure_workflow_store(FakeWorkflowStore())
        http = TestClient(app)
        authorization = http.get(
            "/api/v1/organizations/demo-org/connections/xero/authorize", headers={"Authorization": "Bearer token"}
        ).json()
        first = http.get("/api/v1/connections/xero/callback", params={"state": authorization["state"], "code": "code"})
        second = http.get("/api/v1/connections/xero/callback", params={"state": authorization["state"], "code": "code"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)


if __name__ == "__main__":
    unittest.main()
