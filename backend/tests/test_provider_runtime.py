from datetime import date
import unittest

from app.evidence import EvidenceScope
from app.provider_runtime import (
    GmailHttpClient,
    GoogleDriveHttpClient,
    JsonResponse,
    PlaidHttpSandboxClient,
    RuntimeConfigError,
    XeroBaselineHttpClient,
    StaticSecretResolver,
    XeroDemoHttpClient,
)
from app.providers import ProviderReadError


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, url, headers, payload=None):
        self.calls.append((method, url, headers, payload))
        return self.responses[len(self.calls) - 1]


class FakeOAuthClient:
    def __init__(self):
        self.calls = 0

    def access_token(self):
        self.calls += 1
        return "oauth-access-token"


def resolver():
    return StaticSecretResolver(
        {
            "secret://xero/access": "xero-token",
            "secret://plaid/client": "plaid-secret",
            "secret://google/access": "google-token",
        }
    )


class RuntimeProviderTests(unittest.TestCase):
    def test_xero_demo_client_maps_page_and_keeps_tenant_header(self):
        transport = FakeTransport(
            [JsonResponse(200, {"Invoices": [{"InvoiceID": "inv-1"}]}, {"x-request-id": "x-1"})]
        )
        client = XeroDemoHttpClient("tenant-1", "secret://xero/access", resolver(), transport, page_size=100)
        page = client.get_page(1)
        self.assertEqual(page.records[0]["InvoiceID"], "inv-1")
        self.assertEqual(page.next_page, None)
        self.assertEqual(transport.calls[0][2]["Xero-tenant-id"], "tenant-1")

    def test_xero_demo_client_uses_oauth_token_provider(self):
        transport = FakeTransport([JsonResponse(200, {"Invoices": [{"InvoiceID": "inv-1"}]}, {})])
        oauth = FakeOAuthClient()
        client = XeroDemoHttpClient("tenant-1", "secret://unused", resolver(), transport, oauth_client=oauth)
        client.get_page(1)
        self.assertEqual(oauth.calls, 1)
        self.assertEqual(transport.calls[0][2]["Authorization"], "Bearer oauth-access-token")

    def test_xero_baseline_collects_demo_identity_and_required_account_ids(self):
        transport = FakeTransport(
            [
                JsonResponse(200, {"Organisations": [{"IsDemoCompany": True}]}, {}),
                JsonResponse(
                    200,
                    {
                        "Accounts": [
                            {"Code": "200", "AccountID": "account-id-200"},
                            {"Code": "610", "AccountID": "account-id-610"},
                        ]
                    },
                    {},
                ),
            ]
        )
        baseline = XeroBaselineHttpClient("tenant-1", lambda: "oauth-access-token", transport).collect()
        self.assertEqual(baseline.tenant_id, "tenant-1")
        self.assertEqual(baseline.provider_ids["account-200"], "account-id-200")
        self.assertEqual(baseline.provider_ids["account-610"], "account-id-610")

    def test_xero_baseline_rejects_non_demo_tenant(self):
        transport = FakeTransport([JsonResponse(200, {"Organisations": [{"IsDemoCompany": False}]}, {})])
        with self.assertRaises(ProviderReadError):
            XeroBaselineHttpClient("tenant-1", lambda: "oauth-access-token", transport).collect()

    def test_plaid_sandbox_client_maps_cursor_changes(self):
        transport = FakeTransport(
            [
                JsonResponse(
                    200,
                    {
                        "added": [{"transaction_id": "tx-1", "amount": 2}],
                        "modified": [],
                        "removed": [{"transaction_id": "tx-old"}],
                        "next_cursor": "cursor-1",
                        "has_more": False,
                    },
                    {"x-request-id": "p-1"},
                )
            ]
        )
        client = PlaidHttpSandboxClient("client-1", "secret://plaid/client", resolver(), transport)
        page = client.sync("access-token", None)
        self.assertEqual(page.next_cursor, "cursor-1")
        self.assertEqual(page.removed[0]["transaction_id"], "tx-old")
        self.assertEqual(transport.calls[0][3]["secret"], "plaid-secret")

    def test_google_clients_require_scoped_server_secrets(self):
        scope = EvidenceScope(
            frozenset({"folder-1"}),
            "mailbox@example.test",
            frozenset({"LABEL_CLOSE"}),
            date(2026, 7, 1),
            date(2026, 7, 31),
        )
        drive_transport = FakeTransport(
            [
                JsonResponse(
                    200,
                    {"files": [{"id": "doc-1", "name": "close.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-07-10T00:00:00Z", "parents": ["folder-1"], "md5Checksum": "hash-1"}]},
                    {},
                )
            ]
        )
        drive = GoogleDriveHttpClient("secret://google/access", resolver(), drive_transport)
        self.assertEqual(drive.search_evidence(scope)[0].resource_id, "doc-1")
        gmail_transport = FakeTransport(
            [
                JsonResponse(200, {"messages": [{"id": "msg-1"}]}, {}),
                JsonResponse(200, {"id": "msg-1", "threadId": "thread-1", "internalDate": "1783641600000", "labelIds": ["LABEL_CLOSE"], "payload": {"headers": [{"name": "From", "value": "sender@example.test"}, {"name": "Subject", "value": "Support"}]}}, {}),
            ]
        )
        gmail = GmailHttpClient("secret://google/access", resolver(), gmail_transport)
        result = gmail.search_evidence(scope)
        self.assertEqual(result[0].message_id, "msg-1")
        self.assertEqual(result[0].subject, "Support")

    def test_secret_references_cannot_be_plaintext(self):
        with self.assertRaises(RuntimeConfigError):
            resolver().resolve("raw-token")

    def test_invalid_provider_pagination_and_message_shape_fail_closed(self):
        xero = XeroDemoHttpClient(
            "tenant-1",
            "secret://xero/access",
            resolver(),
            FakeTransport([JsonResponse(200, {"Invoices": [], "next_page": "later"}, {})]),
        )
        with self.assertRaises(ProviderReadError):
            xero.get_page(1)
        scope = EvidenceScope(
            frozenset({"folder-1"}),
            "mailbox@example.test",
            frozenset({"LABEL_CLOSE"}),
            date(2026, 7, 1),
            date(2026, 7, 31),
        )
        gmail = GmailHttpClient(
            "secret://google/access",
            resolver(),
            FakeTransport(
                [
                    JsonResponse(200, {"messages": [{"id": "msg-1"}]}, {}),
                    JsonResponse(200, {"internalDate": "1783641600000", "payload": {"headers": {}}}, {}),
                ]
            ),
        )
        with self.assertRaises(ProviderReadError):
            gmail.search_evidence(scope)


if __name__ == "__main__":
    unittest.main()
