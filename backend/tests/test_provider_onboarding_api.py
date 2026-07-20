import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.close_mapping import PersistedCloseMapping
from app.google_oauth import FormResponse, GoogleOAuthClient, GoogleOAuthConfig
from app.main import (
    app,
    configure_auth_verifier,
    configure_google_oauth,
    configure_plaid_link,
    configure_workflow_store,
    connections,
    xero_oauth_sessions,
)
from app.plaid_link import JsonResponse, PlaidLinkClient, PlaidLinkConfig
from app.supabase_auth import SupabaseUser


class FakeVerifier:
    def authenticate(self, token):
        return SupabaseUser("controller-1", "controller@example.test", "https://demo.supabase.co/auth/v1")


class FakeSecrets:
    def __init__(self):
        self.values = {"secret://google/client": "google-secret", "secret://plaid/client": "plaid-secret"}

    def resolve(self, ref):
        return self.values[ref]

    def store(self, ref, value):
        self.values[ref] = value


class GoogleTransport:
    def post(self, url, headers, form):
        return FormResponse(200, {"access_token": "google-access", "refresh_token": "google-refresh", "expires_in": 3600, "scope": "scope-a scope-b"})


class PlaidTransport:
    def post(self, url, payload):
        if url.endswith("/link/token/create"):
            return JsonResponse(200, {"link_token": "link-token"})
        if url.endswith("/item/public_token/exchange"):
            return JsonResponse(200, {"access_token": "plaid-access", "item_id": "item-1"})
        if url.endswith("/accounts/get"):
            return JsonResponse(200, {"accounts": [{"account_id": "account-1", "name": "Operating"}]})
        raise AssertionError(url)


class FakeStore:
    def __init__(self):
        self.connections = []
        self.mapping = None

    def membership_role(self, organization_id, issuer, subject):
        return "controller" if organization_id == "org-1" else None

    def upsert_connection(self, **kwargs):
        self.connections.append(kwargs)
        health = kwargs["connection_health"]
        return health

    def active_close_mapping(self, organization_id):
        return self.mapping

    def save_close_mapping(self, **kwargs):
        self.mapping = PersistedCloseMapping(
            "mapping-1", kwargs["organization_id"], 1, "active", kwargs["mapping"].as_dict(), kwargs["approved_by_subject"], datetime.now(timezone.utc)
        )
        return self.mapping


def google_client():
    return GoogleOAuthClient(
        GoogleOAuthConfig(
            "google-client", "secret://google/client", "http://localhost:8000/api/v1/connections/google/callback", ("scope-a", "scope-b"),
        ),
        FakeSecrets(),
        GoogleTransport(),
    )


def plaid_client():
    return PlaidLinkClient(
        PlaidLinkConfig("plaid-client", "secret://plaid/client", "https://api.example.com/api/v1/webhooks/plaid"),
        FakeSecrets(),
        PlaidTransport(),
    )


class ProviderOnboardingApiTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        configure_auth_verifier(FakeVerifier())
        configure_workflow_store(self.store)
        configure_google_oauth(google_client())
        configure_plaid_link(plaid_client())
        connections._connections.clear()
        self.http = TestClient(app)
        self.headers = {"Authorization": "Bearer signed-token"}

    def tearDown(self):
        configure_auth_verifier(None)
        configure_workflow_store(None)
        configure_google_oauth(None)
        configure_plaid_link(None)
        connections._connections.clear()
        xero_oauth_sessions._sessions.clear()

    def test_google_callback_stores_connections_without_returning_credentials(self):
        authorization = self.http.get(
            "/api/v1/organizations/org-1/connections/google/authorize", headers=self.headers
        )
        self.assertEqual(authorization.status_code, 200)
        result = self.http.get(
            "/api/v1/connections/google/callback",
            params={"state": authorization.json()["state"], "code": "one-time-code"},
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"status": "authorized", "organization_id": "org-1"})
        self.assertEqual({item["connection_health"].provider for item in self.store.connections}, {"drive", "gmail"})
        self.assertNotIn("google-access", result.text)
        self.assertNotIn("google-refresh", result.text)

    def test_plaid_exchange_returns_connection_metadata_not_access_token(self):
        token = self.http.get(
            "/api/v1/organizations/org-1/connections/plaid/link-token", headers=self.headers
        )
        self.assertEqual(token.json()["link_token"], "link-token")
        result = self.http.post(
            "/api/v1/organizations/org-1/connections/plaid/exchange",
            headers=self.headers,
            json={"public_token": "public-token", "selected_account_ids": ["account-1"]},
        )
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()[0]["provider_tenant_or_account_id"], "account-1")
        self.assertNotIn("plaid-access", result.text)

    def test_controller_can_save_close_mapping(self):
        payload = {
            "xero_tenant_id": "tenant-1",
            "bank_mappings": [{"plaid_account_id": "account-1", "xero_account_code": "1000", "xero_account_name": "Operating"}],
            "matching_rules": {"date_window_days": 3, "fee_tolerance": "1", "materiality_threshold": "100", "pending_policy": "exception", "max_aggregate_size": 10},
            "permitted_journal_account_codes": ["1000"],
            "evidence": {"drive_folder_ids": ["folder-1"], "gmail_mailbox": "close@example.test", "gmail_labels": ["MONTH_END"], "allowed_recipients": ["controller@example.test"], "retention_policy_version": "v1"},
        }
        result = self.http.post("/api/v1/organizations/org-1/close-mapping", headers=self.headers, json=payload)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()["version"], 1)
        self.assertNotIn("token", result.text.lower())


if __name__ == "__main__":
    unittest.main()
