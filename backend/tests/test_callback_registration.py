import unittest

from fastapi.testclient import TestClient

from app import main
from app.main import app, configure_xero_oauth, connections, xero_oauth_sessions
from app.xero_oauth import (
    FormResponse,
    TenantResponse,
    XeroOAuthClient,
    XeroOAuthConfig,
)


class FakeSecrets:
    def __init__(self):
        self.values = {
            "secret://xero/demo/client-secret": "client-secret",
            "secret://xero/demo/refresh-token": "old-refresh",
        }

    def resolve(self, ref):
        return self.values[ref]

    def store(self, ref, value):
        self.values[ref] = value


class FakeFormTransport:
    def post(self, url, headers, form):
        return FormResponse(
            200,
            {"access_token": "access", "refresh_token": "rotated", "expires_in": 1800, "token_type": "Bearer"},
            {},
        )


class FakeTenantTransport:
    """Returns two granted tenants so multi-tenant registration is exercised."""

    def get(self, url, headers):
        return TenantResponse(
            200,
            [
                {"id": "conn-1", "tenantId": "tenant-aaa", "tenantType": "ORGANISATION", "tenantName": "Alpha"},
                {"id": "conn-2", "tenantId": "tenant-bbb", "tenantType": "ORGANISATION", "tenantName": "Beta"},
            ],
        )


def build_client():
    config = XeroOAuthConfig(
        "client-id",
        "secret://xero/demo/client-secret",
        "secret://xero/demo/refresh-token",
        "http://localhost:8000/api/v1/connections/xero/callback",
        ("offline_access", "accounting.settings.read"),
    )
    return XeroOAuthClient(config, FakeSecrets(), FakeFormTransport(), FakeTenantTransport())


class CallbackRegistrationTests(unittest.TestCase):
    def setUp(self):
        self._original_getenv = main.os.getenv
        configure_xero_oauth(build_client())
        connections._connections.clear()

    def tearDown(self):
        main.os.getenv = self._original_getenv
        configure_xero_oauth(None)
        xero_oauth_sessions._sessions.clear()
        connections._connections.clear()

    def _set_allowlist(self, value):
        original = self._original_getenv

        def fake_getenv(key, default=None):
            if key == "ACCOUNTINGOS_XERO_TENANT_ALLOWLIST":
                return value
            return original(key, default)

        main.os.getenv = fake_getenv

    def _run_flow(self):
        http = TestClient(app)
        authorize = http.get("/api/v1/organizations/demo-org/connections/xero/authorize").json()
        return http.get(
            "/api/v1/connections/xero/callback",
            params={"state": authorize["state"], "code": "one-time-code"},
        )

    def test_registers_every_granted_tenant_by_default(self):
        # No allowlist configured: both granted tenants are registered.
        callback = self._run_flow()
        self.assertEqual(callback.status_code, 200)
        registered = connections.for_organization("demo-org")
        self.assertEqual({c.provider_tenant_or_account_id for c in registered}, {"tenant-aaa", "tenant-bbb"})
        self.assertTrue(all(c.provider == "xero" for c in registered))

    def test_allowlist_filters_registration_to_named_tenants(self):
        self._set_allowlist("tenant-bbb")
        callback = self._run_flow()
        self.assertEqual(callback.status_code, 200)
        registered = connections.for_organization("demo-org")
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0].provider_tenant_or_account_id, "tenant-bbb")

    def test_placeholder_allowlist_is_ignored(self):
        # A leftover ``replace-`` placeholder must not silently block everything.
        self._set_allowlist("replace-with-tenant-id")
        callback = self._run_flow()
        self.assertEqual(callback.status_code, 200)
        registered = connections.for_organization("demo-org")
        self.assertEqual(len(registered), 2)

    def test_allowlist_accepts_comma_and_whitespace_separators(self):
        self._set_allowlist("tenant-aaa, tenant-bbb")
        callback = self._run_flow()
        self.assertEqual(callback.status_code, 200)
        self.assertEqual(len(connections.for_organization("demo-org")), 2)


if __name__ == "__main__":
    unittest.main()
