import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.domain import DeploymentConfig, SourceBatch, SourceRecordVersion
from app.durable_worker import (
    DemoSourceSyncExecutor,
    DurableWorkflowWorker,
    ProductionSourceSyncExecutor,
    ReconciliationMappingGateExecutor,
    TaskBlocked,
)
from app.supabase_db import PersistedCloseRun, PersistedConnection, PersistedTask


class FakeStore:
    def __init__(self, task=None):
        self.task = task
        self.completed = []
        self.blocked = []

    def claim_next_task(self, worker_id, *, lease_seconds=60):
        task, self.task = self.task, None
        return task

    def complete_task(self, task, worker_id):
        self.completed.append((task.task_id, worker_id))

    def block_task(self, task, worker_id, error):
        self.blocked.append((task.task_id, worker_id, error))


class SucceedingExecutor:
    def execute(self, task):
        return None


class BlockingExecutor:
    def execute(self, task):
        raise TaskBlocked("Xero Demo Company is not connected")


class FailingExecutor:
    def execute(self, task):
        raise RuntimeError("access token should not be exposed")


class DurableWorkerTests(unittest.TestCase):
    def setUp(self):
        self.task = PersistedTask("task-1", "run-1", "preflight", "running", 1, "worker-1", None, None)

    def test_worker_completes_claimed_task(self):
        store = FakeStore(self.task)
        result = DurableWorkflowWorker(store, SucceedingExecutor(), worker_id="worker-1").process_once()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(store.completed, [("task-1", "worker-1")])

    def test_worker_exposes_actionable_blocker(self):
        store = FakeStore(self.task)
        result = DurableWorkflowWorker(store, BlockingExecutor(), worker_id="worker-1").process_once()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(store.blocked[0][2], "Xero Demo Company is not connected")

    def test_worker_hides_unexpected_error_details(self):
        store = FakeStore(self.task)
        result = DurableWorkflowWorker(store, FailingExecutor(), worker_id="worker-1").process_once()
        self.assertEqual(result.status, "blocked")
        self.assertNotIn("access token", result.error)

    def test_worker_returns_idle_without_a_claim(self):
        result = DurableWorkflowWorker(FakeStore(), SucceedingExecutor(), worker_id="worker-1").process_once()
        self.assertEqual(result.status, "idle")

    def test_reconciliation_mapping_gap_is_a_clear_workflow_blocker(self):
        task = PersistedTask("task-2", "run-1", "reconcile", "running", 1, "worker-1", None, None)
        store = FakeStore(task)
        result = DurableWorkflowWorker(store, ReconciliationMappingGateExecutor(), worker_id="worker-1").process_once()
        self.assertEqual(result.status, "blocked")
        self.assertIn("accountant-approved bank-to-ledger mapping", result.error or "")
        self.assertNotIn("no worker handler", result.error or "")

    def test_source_sync_commits_the_exact_batches_that_created_the_snapshot(self):
        deployment = DeploymentConfig("demo-us", "demo", "synthetic", "US", "USD", "controller")
        run = PersistedCloseRun("run-1", "org-1", "2026-07-01", "2026-07-31", "synchronizing", "demo", "synthetic", None, None)
        xero_batch = SourceBatch(
            "11111111-1111-1111-1111-111111111111",
            "xero",
            "demo",
            "page-1",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            (SourceRecordVersion("xero:invoice-1:abc", "xero", "invoice-1", "abc", datetime(2026, 7, 1, tzinfo=timezone.utc), '{"id":"invoice-1"}'),),
        )
        plaid_batch = SourceBatch(
            "22222222-2222-2222-2222-222222222222",
            "plaid",
            "sandbox",
            "cursor:1",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            (SourceRecordVersion("plaid:transaction-1:def", "plaid", "transaction-1", "def", datetime(2026, 7, 1, tzinfo=timezone.utc), '{"transaction_id":"transaction-1","account_id":"account-1"}'),),
        )

        class SourceStore:
            def __init__(self):
                self.persisted = None

            def get_close_run(self, run_id):
                return run

            def connections_for_organization(self, organization_id):
                return (
                    PersistedConnection("conn-1", "org-1", "xero", "demo", "tenant-1", "healthy", (), None, None, None, None),
                )

            def persist_source_snapshot(self, **kwargs):
                self.persisted = kwargs

        class FakeSecrets:
            def resolve(self, reference):
                return "secret-value"

        class FakeXeroConfig:
            client_secret_ref = "secret://xero/demo/client-secret"
            refresh_token_secret_ref = "secret://xero/demo/refresh-token"

        class Adapter:
            def __init__(self, batch):
                self.batch = batch

            def read_batch(self):
                return self.batch

        store = SourceStore()
        env = {
            "ACCOUNTINGOS_XERO_DEMO_TENANT_ID": "tenant-1",
            "PLAID_CLIENT_ID": "client",
            "PLAID_SECRET_REF": "secret://plaid/demo/client-secret",
            "PLAID_ACCESS_TOKEN_REF": "secret://plaid/demo/access-token",
            "PLAID_ITEM_ID": "item-1",
        }
        with (
            patch("app.durable_worker.XeroOAuthConfig.from_environment", return_value=FakeXeroConfig()),
            patch("app.durable_worker.secret_store_from_environment", return_value=FakeSecrets()),
            patch("app.durable_worker.XeroOAuthClient"),
            patch("app.durable_worker.XeroDemoHttpClient"),
            patch("app.durable_worker.PlaidHttpSandboxClient"),
            patch("app.durable_worker.XeroDemoAdapter", return_value=Adapter(xero_batch)),
            patch("app.durable_worker.PlaidSandboxAdapter", return_value=Adapter(plaid_batch)),
        ):
            DemoSourceSyncExecutor(store, deployment, env).execute(
                PersistedTask("task-1", "run-1", "synchronize_sources", "running", 1, "worker-1", None, None)
            )
        self.assertEqual(store.persisted["batches"], (xero_batch, plaid_batch))
        self.assertEqual(store.persisted["provider_identities"], {"xero": "tenant-1", "plaid": "item-1"})
        self.assertEqual({item.provider for item in store.persisted["snapshot"].records}, {"xero", "plaid"})

    def test_production_source_sync_requires_approved_tenant_and_accounts(self):
        deployment = DeploymentConfig("us-production", "production", "live", "US", "USD", "controller")
        run = PersistedCloseRun("run-1", "org-1", "2026-07-01", "2026-07-31", "synchronizing", "production", "live", None, None)
        xero_batch = SourceBatch(
            "11111111-1111-1111-1111-111111111111",
            "xero",
            "production",
            "page-1",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            (SourceRecordVersion("xero:invoice-1:abc", "xero", "invoice-1", "abc", datetime(2026, 7, 1, tzinfo=timezone.utc), '{"id":"invoice-1"}'),),
        )
        plaid_batch = SourceBatch(
            "22222222-2222-2222-2222-222222222222",
            "plaid",
            "production",
            "cursor:1",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            (SourceRecordVersion("plaid:transaction-1:def", "plaid", "transaction-1", "def", datetime(2026, 7, 1, tzinfo=timezone.utc), '{"transaction_id":"transaction-1","account_id":"account-1"}'),),
        )

        class SourceStore:
            def __init__(self):
                self.persisted = None

            def get_close_run(self, run_id):
                return run

            def connections_for_organization(self, organization_id):
                return (
                    PersistedConnection("conn-1", "org-1", "xero", "production", "tenant-1", "healthy", (), None, None, None, None),
                )

            def persist_source_snapshot(self, **kwargs):
                self.persisted = kwargs

        class FakeSecrets:
            def resolve(self, reference):
                return "secret-value"

        class FakeXeroConfig:
            client_secret_ref = "secret://xero/production/client-secret"
            refresh_token_secret_ref = "secret://xero/production/refresh-token"

        class Adapter:
            def __init__(self, batch):
                self.batch = batch

            def read_batch(self):
                return self.batch

        store = SourceStore()
        env = {
            "ACCOUNTINGOS_XERO_TENANT_ID": "tenant-1",
            "ACCOUNTINGOS_PLAID_SELECTED_ACCOUNT_IDS": "account-1",
            "PLAID_CLIENT_ID": "client",
            "PLAID_SECRET_REF": "secret://plaid/production/client-secret",
            "PLAID_ACCESS_TOKEN_REF": "secret://plaid/production/access-token",
            "PLAID_ITEM_ID": "item-1",
        }
        with (
            patch("app.durable_worker.XeroOAuthConfig.from_environment", return_value=FakeXeroConfig()),
            patch("app.durable_worker.secret_store_from_environment", return_value=FakeSecrets()),
            patch("app.durable_worker.XeroOAuthClient"),
            patch("app.durable_worker.XeroProductionHttpClient"),
            patch("app.durable_worker.PlaidProductionHttpClient"),
            patch("app.durable_worker.XeroProductionAdapter", return_value=Adapter(xero_batch)),
            patch("app.durable_worker.PlaidProductionAdapter", return_value=Adapter(plaid_batch)),
        ):
            ProductionSourceSyncExecutor(store, deployment, env).execute(
                PersistedTask("task-1", "run-1", "synchronize_sources", "running", 1, "worker-1", None, None)
            )
        self.assertEqual(store.persisted["provider_identities"], {"xero": "tenant-1", "plaid": "item-1"})

    def test_production_source_sync_rejects_unselected_plaid_account(self):
        deployment = DeploymentConfig("us-production", "production", "live", "US", "USD", "controller")
        run = PersistedCloseRun("run-1", "org-1", "2026-07-01", "2026-07-31", "synchronizing", "production", "live", None, None)

        class SourceStore:
            def get_close_run(self, run_id):
                return run

            def connections_for_organization(self, organization_id):
                return (PersistedConnection("conn-1", "org-1", "xero", "production", "tenant-1", "healthy", (), None, None, None, None),)

            def persist_source_snapshot(self, **kwargs):
                raise AssertionError("unapproved account must not be persisted")

        class FakeSecrets:
            def resolve(self, reference):
                return "secret-value"

        class FakeXeroConfig:
            client_secret_ref = "secret://xero/production/client-secret"
            refresh_token_secret_ref = "secret://xero/production/refresh-token"

        xero_batch = SourceBatch("11111111-1111-1111-1111-111111111111", "xero", "production", "page-1", datetime(2026, 7, 1, tzinfo=timezone.utc), ())
        plaid_batch = SourceBatch("22222222-2222-2222-2222-222222222222", "plaid", "production", "cursor:1", datetime(2026, 7, 1, tzinfo=timezone.utc), (SourceRecordVersion("plaid:transaction-1:def", "plaid", "transaction-1", "def", datetime(2026, 7, 1, tzinfo=timezone.utc), '{"transaction_id":"transaction-1","account_id":"unapproved"}'),))

        class Adapter:
            def __init__(self, batch):
                self.batch = batch

            def read_batch(self):
                return self.batch

        env = {"ACCOUNTINGOS_XERO_TENANT_ID": "tenant-1", "ACCOUNTINGOS_PLAID_SELECTED_ACCOUNT_IDS": "account-1", "PLAID_CLIENT_ID": "client", "PLAID_SECRET_REF": "secret://plaid/production/client-secret", "PLAID_ACCESS_TOKEN_REF": "secret://plaid/production/access-token", "PLAID_ITEM_ID": "item-1"}
        with (
            patch("app.durable_worker.XeroOAuthConfig.from_environment", return_value=FakeXeroConfig()),
            patch("app.durable_worker.secret_store_from_environment", return_value=FakeSecrets()),
            patch("app.durable_worker.XeroOAuthClient"),
            patch("app.durable_worker.XeroProductionHttpClient"),
            patch("app.durable_worker.PlaidProductionHttpClient"),
            patch("app.durable_worker.XeroProductionAdapter", return_value=Adapter(xero_batch)),
            patch("app.durable_worker.PlaidProductionAdapter", return_value=Adapter(plaid_batch)),
        ):
            with self.assertRaisesRegex(TaskBlocked, "outside the approved account selection"):
                ProductionSourceSyncExecutor(SourceStore(), deployment, env).execute(
                    PersistedTask("task-1", "run-1", "synchronize_sources", "running", 1, "worker-1", None, None)
                )


if __name__ == "__main__":
    unittest.main()
