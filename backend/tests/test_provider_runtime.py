from datetime import date
import unittest

from app.evidence import EvidenceScope
from app.provider_runtime import (
    GmailHttpClient,
    GoogleDriveHttpClient,
    JsonResponse,
    PlaidHttpSandboxClient,
    PlaidProductionHttpClient,
    RuntimeConfigError,
    XeroBaselineHttpClient,
    StaticSecretResolver,
    XeroDemoHttpClient,
    XeroProductionHttpClient,
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

    def test_production_clients_use_production_endpoints_and_tags(self):
        xero_transport = FakeTransport(
            [
                JsonResponse(200, {"BankTransactions": [{"BankTransactionID": "bank-1", "Total": "12.50"}]}, {}),
                JsonResponse(200, {"Payments": [{"PaymentID": "payment-1", "Amount": "4.50"}]}, {}),
                JsonResponse(200, {"ManualJournals": []}, {}),
                JsonResponse(200, {"Accounts": [{"AccountID": "account-1", "Code": "1000", "CurrencyCode": "USD"}]}, {}),
            ]
        )
        xero = XeroProductionHttpClient("tenant-1", "secret://xero/access", resolver(), xero_transport)
        xero_page = xero.get_page(1)
        self.assertEqual(xero_page.provider_environment, "production")
        self.assertEqual({item["record_type"] for item in xero_page.records}, {"bank_transaction", "payment", "account"})
        self.assertTrue(all("Invoices" not in call[1] for call in xero_transport.calls))
        plaid_transport = FakeTransport(
            [JsonResponse(200, {"added": [], "modified": [], "removed": [], "next_cursor": "cursor-1", "has_more": False}, {})]
        )
        plaid = PlaidProductionHttpClient("client-1", "secret://plaid/client", resolver(), plaid_transport)
        self.assertEqual(plaid.sync("access-token", None).provider_environment, "production")
        self.assertTrue(plaid_transport.calls[0][1].startswith("https://production.plaid.com/"))

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
                    {
                        "files": [{"id": "doc-1", "name": "close.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-07-10T00:00:00Z", "parents": ["folder-1"], "md5Checksum": "hash-1"}],
                        "nextPageToken": "drive-page-2",
                    },
                    {},
                ),
                JsonResponse(
                    200,
                    {"files": [{"id": "doc-2", "name": "support.xlsx", "mimeType": "application/vnd.ms-excel", "modifiedTime": "2026-07-11T00:00:00Z", "parents": ["folder-1"], "md5Checksum": "hash-2"}]},
                    {},
                )
            ]
        )
        drive = GoogleDriveHttpClient("secret://google/access", resolver(), drive_transport)
        self.assertEqual([item.resource_id for item in drive.search_evidence(scope)], ["doc-1", "doc-2"])
        self.assertIn("pageToken=drive-page-2", drive_transport.calls[1][1])
        gmail_transport = FakeTransport(
            [
                JsonResponse(200, {"labels": [{"id": "Label_5", "name": "LABEL_CLOSE"}]}, {}),
                JsonResponse(200, {"messages": [{"id": "msg-1"}], "nextPageToken": "gmail-page-2"}, {}),
                JsonResponse(200, {"messages": [{"id": "msg-2"}]}, {}),
                JsonResponse(200, {"id": "msg-1", "threadId": "thread-1", "internalDate": "1783641600000", "labelIds": ["Label_5"], "payload": {"headers": [{"name": "From", "value": "sender@example.test"}, {"name": "Subject", "value": "Support"}]}}, {}),
                JsonResponse(200, {"id": "msg-2", "threadId": "thread-2", "internalDate": "1783728000000", "labelIds": ["Label_5"], "payload": {"headers": [{"name": "From", "value": "sender@example.test"}, {"name": "Subject", "value": "Support 2"}]}}, {}),
            ]
        )
        gmail = GmailHttpClient("secret://google/access", resolver(), gmail_transport)
        result = gmail.search_evidence(scope)
        self.assertEqual([item.message_id for item in result], ["msg-1", "msg-2"])
        self.assertEqual(result[0].subject, "Support")
        self.assertEqual(result[0].labels, frozenset({"LABEL_CLOSE"}))
        self.assertIn("labelIds=Label_5", gmail_transport.calls[1][1])
        self.assertIn("before%3A2026-08-01", gmail_transport.calls[1][1])
        self.assertIn("pageToken=gmail-page-2", gmail_transport.calls[2][1])

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
