import base64
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from app.xero_oauth import FormResponse, XeroOAuthClient, XeroOAuthConfig, XeroOAuthError


class FakeSecrets:
    def __init__(self):
        self.values = {"secret://xero/demo/client-secret": "client-secret", "secret://xero/demo/refresh-token": "old-refresh"}
        self.stored = []

    def resolve(self, secret_ref):
        value = self.values.get(secret_ref, "")
        if not value:
            raise XeroOAuthError("secret unavailable")
        return value

    def store(self, secret_ref, value):
        self.values[secret_ref] = value
        self.stored.append((secret_ref, value))


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers, form):
        self.calls.append((url, headers, form))
        return self.response


def config():
    return XeroOAuthConfig(
        "client-id",
        "secret://xero/demo/client-secret",
        "secret://xero/demo/refresh-token",
        "http://localhost:8000/api/v1/connections/xero/callback",
        ("offline_access", "accounting.settings.read"),
    )


class XeroOAuthTests(unittest.TestCase):
    def test_authorization_url_contains_pkce_and_exact_scopes(self):
        client = XeroOAuthClient(config(), FakeSecrets(), FakeTransport(FormResponse(200, {}, {})))
        parsed = parse_qs(urlsplit(client.authorization_url("state-1", "challenge-1")).query)
        self.assertEqual(parsed["client_id"], ["client-id"])
        self.assertEqual(parsed["code_challenge_method"], ["S256"])
        self.assertEqual(parsed["scope"], ["offline_access accounting.settings.read"])
        self.assertNotIn("client-secret", str(parsed))

    def test_exchange_uses_basic_auth_and_stores_rotated_refresh_token(self):
        secrets = FakeSecrets()
        transport = FakeTransport(FormResponse(200, {"access_token": "access-1", "refresh_token": "refresh-2", "expires_in": 1800, "token_type": "Bearer"}, {}))
        token = XeroOAuthClient(config(), secrets, transport).exchange_code("one-time-code", "verifier")
        self.assertEqual(token.access_token, "access-1")
        self.assertEqual(secrets.values["secret://xero/demo/refresh-token"], "refresh-2")
        auth = transport.calls[0][1]["Authorization"]
        self.assertEqual(base64.b64decode(auth.removeprefix("Basic ")).decode(), "client-id:client-secret")
        self.assertNotIn("client-secret", str(transport.calls[0][2]))

    def test_refresh_reads_current_token_and_replaces_it(self):
        secrets = FakeSecrets()
        transport = FakeTransport(FormResponse(200, {"access_token": "access-2", "refresh_token": "refresh-3", "expires_in": 1800, "token_type": "Bearer"}, {}))
        token = XeroOAuthClient(config(), secrets, transport).refresh()
        self.assertEqual(token.refresh_token, "refresh-3")
        self.assertEqual(transport.calls[0][2], {"grant_type": "refresh_token", "refresh_token": "old-refresh"})

    def test_access_token_refreshes_only_when_near_expiry(self):
        secrets = FakeSecrets()
        transport = FakeTransport(FormResponse(200, {"access_token": "access-2", "refresh_token": "refresh-3", "expires_in": 120, "token_type": "Bearer"}, {}))
        client = XeroOAuthClient(config(), secrets, transport)
        issued = datetime(2026, 7, 18, tzinfo=timezone.utc)
        client._cache(client.refresh(), now=issued)
        self.assertEqual(client.access_token(now=issued + timedelta(seconds=20)), "access-2")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(client.access_token(now=issued + timedelta(seconds=70)), "access-2")
        self.assertEqual(len(transport.calls), 2)

    def test_http_error_and_incomplete_rotation_fail_closed(self):
        client = XeroOAuthClient(config(), FakeSecrets(), FakeTransport(FormResponse(401, {"error": "invalid_grant"}, {})))
        with self.assertRaises(XeroOAuthError):
            client.refresh()
        incomplete = XeroOAuthClient(config(), FakeSecrets(), FakeTransport(FormResponse(200, {"access_token": "only-access", "expires_in": 1800}, {})))
        with self.assertRaises(XeroOAuthError):
            incomplete.refresh()

    def test_redirect_and_refresh_scope_rules_are_enforced(self):
        with self.assertRaises(XeroOAuthError):
            XeroOAuthConfig("id", "secret://client", "secret://refresh", "http://127.0.0.1:8000/callback", ("offline_access",))
        with self.assertRaises(XeroOAuthError):
            XeroOAuthConfig("id", "secret://client", "secret://refresh", "https://example.test/callback", ("accounting.settings.read",))


if __name__ == "__main__":
    unittest.main()
