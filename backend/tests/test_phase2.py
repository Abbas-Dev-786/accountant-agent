from datetime import datetime, timezone
import unittest

from app.domain import CloseService, DeploymentConfig, PolicyError, RunState
from app.ingestion import DemoIngestionService
from app.normalization import normalize_provider_record
from app.providers import (
    PlaidCursorState,
    PlaidSandboxAdapter,
    PlaidSyncPage,
    ProviderReadError,
    XeroDemoAdapter,
    XeroPage,
)


class XeroClient:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get_page(self, page):
        self.requests.append(page)
        return self.pages[page]


class PlaidClient:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def sync(self, access_token, cursor):
        self.requests.append((access_token, cursor))
        return self.pages[len(self.requests) - 1]


def demo_deployment():
    return DeploymentConfig("demo-us", "demo", "synthetic", "US", "USD", "controller-1")


class NormalizationTests(unittest.TestCase):
    def test_key_order_does_not_change_content_hash(self):
        observed = datetime(2026, 7, 18, tzinfo=timezone.utc)
        first = normalize_provider_record(
            "plaid", "tx-1", {"amount": 42, "status": "pending"}, fallback_observed_at=observed
        )
        second = normalize_provider_record(
            "plaid", "tx-1", {"status": "pending", "amount": 42}, fallback_observed_at=observed
        )
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.payload_json, '{"amount":42,"status":"pending"}')


class XeroAdapterTests(unittest.TestCase):
    def test_demo_pagination_is_ordered_and_immutable(self):
        client = XeroClient(
            {
                1: XeroPage(1, ({"id": "j-1", "amount": 10},), 2, "tenant-1", request_id="r1"),
                2: XeroPage(2, ({"id": "j-2", "amount": 20},), None, "tenant-1", request_id="r2"),
            }
        )
        batch = XeroDemoAdapter(client, "tenant-1").read_batch()
        self.assertEqual(client.requests, [1, 2])
        self.assertEqual([record.provider_record_id for record in batch.record_versions], ["j-1", "j-2"])
        self.assertTrue(batch.watermark.startswith("page-2|"))

    def test_duplicate_or_cross_tenant_page_is_blocked(self):
        duplicate = XeroClient(
            {1: XeroPage(1, ({"id": "j-1"}, {"id": "j-1"}), None, "tenant-1")}
        )
        with self.assertRaises(ProviderReadError):
            XeroDemoAdapter(duplicate, "tenant-1").read_batch()
        wrong_tenant = XeroClient({1: XeroPage(1, ({"id": "j-1"},), None, "tenant-2")})
        with self.assertRaises(ProviderReadError):
            XeroDemoAdapter(wrong_tenant, "tenant-1").read_batch()

    def test_page_limit_prevents_unbounded_provider_reads(self):
        client = XeroClient({1: XeroPage(1, (), 2, "tenant-1"), 2: XeroPage(2, (), 3, "tenant-1")})
        with self.assertRaises(ProviderReadError):
            XeroDemoAdapter(client, "tenant-1", max_pages=1).read_batch()


class PlaidAdapterTests(unittest.TestCase):
    def test_cursor_sync_applies_added_modified_and_removed(self):
        state = PlaidCursorState(cursor="cursor-0", records={"old": {"id": "old", "amount": 1}})
        client = PlaidClient(
            [
                PlaidSyncPage(
                    cursor="cursor-0",
                    next_cursor="cursor-1",
                    added=({"transaction_id": "new", "amount": 2, "status": "pending"},),
                    modified=({"transaction_id": "old", "amount": 3, "status": "posted"},),
                    removed=("gone",),
                    has_more=False,
                    request_id="p1",
                )
            ]
        )
        batch = PlaidSandboxAdapter(client, "access", state=state).read_batch()
        self.assertEqual(state.cursor, "cursor-1")
        self.assertEqual(state.records["new"]["status"], "pending")
        self.assertEqual(state.records["old"]["status"], "posted")
        self.assertNotIn("gone", state.records)
        self.assertEqual({item.provider_record_id for item in batch.record_versions}, {"new", "old", "gone"})

    def test_failed_page_does_not_commit_cursor_or_records(self):
        state = PlaidCursorState(cursor="cursor-0", records={"old": {"id": "old"}})
        client = PlaidClient(
            [
                PlaidSyncPage(
                    cursor="cursor-0",
                    next_cursor="cursor-1",
                    added=({"transaction_id": "new"},),
                    has_more=True,
                    request_id="p1",
                ),
                PlaidSyncPage(
                    cursor="wrong-cursor",
                    next_cursor="cursor-2",
                    has_more=False,
                    request_id="p2",
                ),
            ]
        )
        with self.assertRaises(ProviderReadError):
            PlaidSandboxAdapter(client, "access", state=state).read_batch()
        self.assertEqual(state.cursor, "cursor-0")
        self.assertEqual(state.records, {"old": {"id": "old"}})


class IngestionTests(unittest.TestCase):
    def test_worker_commits_snapshot_only_after_both_sources_complete(self):
        xero = XeroDemoAdapter(
            XeroClient({1: XeroPage(1, ({"id": "j-1"},), None, "tenant-1")}), "tenant-1"
        )
        plaid = PlaidSandboxAdapter(
            PlaidClient(
                [
                    PlaidSyncPage(
                        cursor=None,
                        next_cursor="cursor-1",
                        added=({"transaction_id": "tx-1", "status": "posted"},),
                    )
                ]
            ),
            "access",
        )
        service = CloseService(demo_deployment())
        run = service.create_run("org-1", "2026-07-01", "2026-07-31")
        snapshot = DemoIngestionService(service, xero, plaid).synchronize(run)
        self.assertEqual(run.state, RunState.RUNNING)
        self.assertEqual(len(snapshot.records), 2)

    def test_failed_source_marks_run_blocked_and_can_be_retried(self):
        xero = XeroDemoAdapter(
            XeroClient({1: XeroPage(1, ({"id": "j-1"},), None, "tenant-1")}), "tenant-1"
        )
        bad_plaid = PlaidSandboxAdapter(PlaidClient([]), "access")
        service = CloseService(demo_deployment())
        run = service.create_run("org-1", "2026-07-01", "2026-07-31")
        with self.assertRaises(ProviderReadError):
            DemoIngestionService(service, xero, bad_plaid).synchronize(run)
        self.assertEqual(run.state, RunState.BLOCKED)


if __name__ == "__main__":
    unittest.main()
