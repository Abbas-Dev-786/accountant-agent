import unittest

from app.xero_oauth import (
    FormResponse,
    TenantResponse,
    XeroOAuthClient,
    XeroOAuthConfig,
    XeroOAuthError,
    XeroTenant,
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
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, dict(headers)))
        return self.response


def build_client(tenant_transport):
    config = XeroOAuthConfig(
        "client-id",
        "secret://xero/demo/client-secret",
        "secret://xero/demo/refresh-token",
        "http://localhost:8000/api/v1/connections/xero/callback",
        ("offline_access", "accounting.settings.read"),
    )
    return XeroOAuthClient(config, FakeSecrets(), FakeFormTransport(), tenant_transport)


class ListTenantsTests(unittest.TestCase):
    def test_parses_tenant_list_and_sends_bearer(self):
        response = TenantResponse(
            200,
            [
                {
                    "id": "conn-1",
                    "tenantId": "tenant-abc",
                    "tenantType": "ORGANISATION",
                    "tenantName": "Demo Company (US)",
                }
            ],
        )
        transport = FakeTenantTransport(response)
        client = build_client(transport)
        tenants = client.list_tenants()
        self.assertEqual(
            tenants,
            (XeroTenant("conn-1", "tenant-abc", "ORGANISATION", "Demo Company (US)"),),
        )
        # Discovery must hit the connections endpoint with a bearer token.
        url, headers = transport.calls[0]
        self.assertEqual(url, "https://api.xero.com/connections")
        self.assertTrue(headers["Authorization"].startswith("Bearer "))

    def test_http_error_raises(self):
        client = build_client(FakeTenantTransport(TenantResponse(403, {})))
        with self.assertRaises(XeroOAuthError):
            client.list_tenants()

    def test_non_list_body_raises(self):
        client = build_client(FakeTenantTransport(TenantResponse(200, {"unexpected": "object"})))
        with self.assertRaises(XeroOAuthError):
            client.list_tenants()

    def test_entry_missing_tenant_id_raises(self):
        client = build_client(FakeTenantTransport(TenantResponse(200, [{"id": "conn-1"}])))
        with self.assertRaises(XeroOAuthError):
            client.list_tenants()


if __name__ == "__main__":
    unittest.main()
