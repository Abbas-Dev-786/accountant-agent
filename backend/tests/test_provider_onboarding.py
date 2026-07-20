import unittest
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from app.close_mapping import (
    BankLedgerMapping,
    CloseMappingDraft,
    EvidenceConfiguration,
    MatchingRules,
    draft_from_mapping,
)
from app.google_oauth import FormResponse, GoogleOAuthClient, GoogleOAuthConfig, GoogleOAuthError
from app.plaid_link import JsonResponse, PlaidLinkClient, PlaidLinkConfig, PlaidLinkError
from app.security import create_oauth_transaction


class FakeSecrets:
    def __init__(self):
        self.values = {"secret://google/client": "google-secret", "secret://plaid/client": "plaid-secret"}

    def resolve(self, ref):
        return self.values[ref]

    def store(self, ref, value):
        self.values[ref] = value


class GoogleTransport:
    def __init__(self, body):
        self.body = body
        self.forms = []

    def post(self, url, headers, form):
        self.forms.append((url, headers, form))
        return FormResponse(200, self.body)


class PlaidTransport:
    def __init__(self):
        self.calls = []

    def post(self, url, payload):
        self.calls.append((url, payload))
        if url.endswith("/link/token/create"):
            return JsonResponse(200, {"link_token": "link-production", "expiration": "2026-07-20T12:00:00Z"})
        if url.endswith("/item/public_token/exchange"):
            return JsonResponse(200, {"access_token": "plaid-access", "item_id": "item-1"})
        if url.endswith("/accounts/get"):
            return JsonResponse(200, {"accounts": [{"account_id": "account-1", "name": "Operating", "mask": "1234"}]})
        raise AssertionError(url)


class CloseMappingTests(unittest.TestCase):
    def test_mapping_is_versionable_json_without_provider_tokens(self):
        draft = CloseMappingDraft(
            "tenant-1",
            (BankLedgerMapping("account-1", "1000", "Operating cash"),),
            MatchingRules(3, Decimal("1.50"), Decimal("100"), "exception", 10),
            ("1000", "2000"),
            EvidenceConfiguration(("folder-1",), "close@example.com", ("MONTH_END",), ("controller@example.com",), "us-v1"),
        )
        configuration = draft.as_dict()
        self.assertEqual(draft_from_mapping(configuration).as_dict(), configuration)
        self.assertNotIn("token", repr(configuration).lower())

    def test_mapping_rejects_duplicate_bank_accounts(self):
        with self.assertRaisesRegex(Exception, "only once"):
            CloseMappingDraft(
                "tenant-1",
                (
                    BankLedgerMapping("account-1", "1000", "Operating cash"),
                    BankLedgerMapping("account-1", "1001", "Reserve cash"),
                ),
                MatchingRules(3, Decimal("0"), Decimal("0"), "exception", 10),
                ("1000",),
                EvidenceConfiguration(("folder-1",), "close@example.com", ("MONTH_END",), ("controller@example.com",), "us-v1"),
            )


class GoogleOAuthTests(unittest.TestCase):
    def _client(self, body):
        config = GoogleOAuthConfig(
            "google-client",
            "secret://google/client",
            "http://localhost:8000/api/v1/connections/google/callback",
            ("scope-a", "scope-b"),
        )
        transport = GoogleTransport(body)
        return GoogleOAuthClient(config, FakeSecrets(), transport), transport

    def test_google_authorization_uses_pkce_and_offline_access(self):
        client, _ = self._client({})
        transaction = create_oauth_transaction("drive", client.config.redirect_uri)
        query = parse_qs(urlsplit(client.authorization_url(transaction)).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["scope"], ["scope-a scope-b"])

    def test_google_exchange_requires_refresh_token_and_keeps_it_server_side(self):
        client, transport = self._client({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        transaction = create_oauth_transaction("drive", client.config.redirect_uri)
        token = client.exchange_code("one-time-code", transaction)
        self.assertEqual(token.refresh_token, "refresh")
        self.assertEqual(transport.forms[0][2]["code_verifier"], transaction.code_verifier)
        missing, _ = self._client({"access_token": "access", "expires_in": 3600})
        with self.assertRaises(GoogleOAuthError):
            missing.exchange_code("one-time-code", transaction)


class PlaidLinkTests(unittest.TestCase):
    def _client(self):
        config = PlaidLinkConfig("plaid-client", "secret://plaid/client", "https://api.example.com/api/v1/webhooks/plaid")
        transport = PlaidTransport()
        return PlaidLinkClient(config, FakeSecrets(), transport), transport

    def test_link_token_is_short_lived_browser_material_only(self):
        client, transport = self._client()
        token, expiration = client.create_link_token("org-1")
        self.assertEqual((token, expiration), ("link-production", "2026-07-20T12:00:00Z"))
        self.assertEqual(transport.calls[0][1]["user"], {"client_user_id": "org-1"})
        self.assertNotIn("plaid-secret", token)

    def test_exchange_verifies_selected_accounts(self):
        client, _ = self._client()
        linked = client.exchange_public_token("public-token", ["account-1"])
        self.assertEqual(linked.item_id, "item-1")
        self.assertEqual([account.account_id for account in linked.accounts], ["account-1"])
        with self.assertRaises(PlaidLinkError):
            client.exchange_public_token("public-token", ["other-account"])


if __name__ == "__main__":
    unittest.main()
