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
    def get(self, url, headers):
        return TenantResponse(
            200,
            [{"id": "conn-1", "tenantId": "demo-tenant-123", "tenantType": "ORGANISATION", "tenantName": "Demo"}],
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

    def _set_demo_tenant(self, value):
        original = self._original_getenv

        def fake_getenv(key, default=None):
            if key == "ACCOUNTINGOS_XERO_DEMO_TENANT_ID":
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

    def test_registers_connection_when_demo_tenant_matches(self):
        self._set_demo_tenant("demo-tenant-123")
        callback = self._run_flow()
        self.assertEqual(callback.status_code, 200)
        registered = connections.for_organization("demo-org")
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0].provider, "xero")
        self.assertEqual(registered[0].provider_tenant_or_account_id, "demo-tenant-123")

    def test_no_registration_when_tenant_unconfigured(self):
        self._set_demo_tenant("replace-with-designated-demo-tenant-id")
        callback = self._run_flow()
        self.assertEqual(callback.status_code, 200)
        # Callback still succeeds; registration is simply skipped.
        self.assertEqual(connections.for_organization("demo-org"), ())


if __name__ == "__main__":
    unittest.main()
