import unittest
from datetime import datetime, timezone
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from app.domain import DeploymentConfig, SourceBatch, SourceRecordVersion
from app.durable_worker import (
    DemoSourceSyncExecutor,
    DurableReconciliationExecutor,
    DurableWorkflowWorker,
    GmailRecoveryActionExecutor,
    GoogleEvidenceExecutor,
    ProductionSourceSyncExecutor,
    ReconciliationMappingGateExecutor,
    TaskBlocked,
)
from app.groq import GroqError
from app.close_mapping import PersistedCloseMapping
from app.providers import ProviderReadError
from app.supabase_db import PersistedCloseRun, PersistedConnection, PersistedTask


class FakeStore:
    def __init__(self, task=None):
        self.task = task
        self.completed = []
        self.blocked = []
        self.retried = []
        self.retry_state = "ready"
        self.renewals = []
        self.renewed = Event()
        self.renew_result = True

    def claim_next_task(self, worker_id, *, lease_seconds=60):
        task, self.task = self.task, None
        return task

    def complete_task(self, task, worker_id):
        self.completed.append((task.task_id, worker_id))

    def renew_task_lease(self, task, worker_id, *, lease_seconds=60):
        self.renewals.append((task.task_id, worker_id, lease_seconds))
        self.renewed.set()
        return self.renew_result

    def block_task(self, task, worker_id, error):
        self.blocked.append((task.task_id, worker_id, error))

    def retry_task(self, task, worker_id, error):
        self.retried.append((task.task_id, worker_id, error))
        return self.retry_state


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

    def test_worker_retries_unexpected_errors_without_exposing_details(self):
        store = FakeStore(self.task)
        result = DurableWorkflowWorker(store, FailingExecutor(), worker_id="worker-1").process_once()
        self.assertEqual(result.status, "retrying")
        self.assertNotIn("access token", result.error)
        self.assertEqual(store.blocked, [])
        self.assertEqual(store.retried[0][:2], ("task-1", "worker-1"))

    def test_worker_reports_failed_when_the_retry_budget_is_exhausted(self):
        store = FakeStore(self.task)
        store.retry_state = "failed"
        result = DurableWorkflowWorker(store, FailingExecutor(), worker_id="worker-1").process_once()
        self.assertEqual(result.status, "failed")

    def test_source_sync_is_a_noop_after_its_snapshot_is_committed(self):
        run = SimpleNamespace(
            run_id="run-1", organization_id="org-1", period_start="2026-07-01", period_end="2026-07-31",
            state="synchronizing", snapshot_id="snapshot-1", deployment_mode="production", data_class="live",
        )
        store = SimpleNamespace(get_close_run=lambda _run_id: run)
        task = PersistedTask("task-1", "run-1", "synchronize_sources", "running", 2, "worker-1", None, None)
        ProductionSourceSyncExecutor(
            store, DeploymentConfig("us-prod", "production", "live", "US", "USD", "controller"), env={},
        ).execute(task)

    def test_worker_returns_idle_without_a_claim(self):
        result = DurableWorkflowWorker(FakeStore(), SucceedingExecutor(), worker_id="worker-1").process_once()
        self.assertEqual(result.status, "idle")

    def test_worker_renews_lease_while_a_slow_task_runs(self):
        class SlowExecutor:
            def __init__(self):
                self.started = Event()
                self.release = Event()

            def execute(self, task):
                self.started.set()
                self.release.wait(1)

        store = FakeStore(self.task)
        executor = SlowExecutor()
        worker = DurableWorkflowWorker(store, executor, worker_id="worker-1", lease_seconds=1, heartbeat_interval_seconds=0.01)
        results = []
        thread = Thread(target=lambda: results.append(worker.process_once()))
        thread.start()
        self.assertTrue(executor.started.wait(1))
        self.assertTrue(store.renewed.wait(1))
        executor.release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, "succeeded")
        self.assertEqual(store.completed, [("task-1", "worker-1")])

    def test_worker_does_not_complete_after_lease_ownership_is_lost(self):
        class SlowExecutor:
            def __init__(self):
                self.started = Event()
                self.release = Event()

            def execute(self, task):
                self.started.set()
                self.release.wait(1)

        store = FakeStore(self.task)
        store.renew_result = False
        executor = SlowExecutor()
        worker = DurableWorkflowWorker(store, executor, worker_id="worker-1", lease_seconds=1, heartbeat_interval_seconds=0.01)
        results = []
        thread = Thread(target=lambda: results.append(worker.process_once()))
        thread.start()
        self.assertTrue(executor.started.wait(1))
        self.assertTrue(store.renewed.wait(1))
        executor.release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, "lease_lost")
        self.assertEqual(store.completed, [])
        self.assertEqual(store.blocked, [])

    def test_groq_unavailability_marks_exceptions_without_blocking_the_close(self):
        class ExplanationStore:
            def __init__(self):
                self.unavailable = []

            def unexplained_exceptions_for_run(self, run_id):
                return ({"id": "exception-1", "facts": []},)

            def mark_exception_explanation_unavailable(self, *, run_id, exception_id):
                self.unavailable.append((run_id, exception_id))

        store = ExplanationStore()
        run = type("Run", (), {"run_id": "run-1"})()
        with patch("app.durable_worker.GroqConfig.from_environment", side_effect=GroqError("unavailable")):
            DurableReconciliationExecutor(store, env={})._explain_open_exceptions(run, {})
        self.assertEqual(store.unavailable, [("run-1", "exception-1")])

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

    def test_evidence_provider_failure_is_a_visible_task_blocker(self):
        run = PersistedCloseRun(
            "run-1", "org-1", "2026-07-01", "2026-07-31", "synchronizing", "production", "live", "snapshot-1", None
        )

        class EvidenceStore:
            def get_close_run(self, run_id):
                return run

            def active_close_mapping(self, organization_id):
                return PersistedCloseMapping(
                    "mapping-1", "org-1", 1, "active",
                    {
                        "xero_tenant_id": "tenant-1",
                        "bank_mappings": [{"plaid_account_id": "account-1", "xero_account_code": "1000"}],
                        "evidence": {
                            "drive_folder_ids": ["folder-1"], "gmail_mailbox": "close@example.test",
                            "gmail_labels": ["CLOSE"],
                        },
                    },
                    "controller",
                )

            def connection_secret_ref(self, organization_id, provider, provider_target):
                return "secret://google/org-1/workspace/refresh-token"

            def persist_evidence_batch(self, **kwargs):
                raise AssertionError("a failed provider read must not persist a partial batch")

        class FakeSecrets:
            def resolve(self, reference):
                return "secret-value"

        with (
            patch("app.durable_worker.secret_store_from_environment", return_value=FakeSecrets()),
            patch("app.durable_worker.GoogleOAuthConfig.from_environment", return_value=object()),
            patch("app.durable_worker.GoogleOAuthClient") as oauth_client,
            patch("app.durable_worker.EvidenceCollector") as collector,
        ):
            collector.return_value.collect.side_effect = ProviderReadError("Gmail page failed")
            with self.assertRaisesRegex(TaskBlocked, "Google evidence collection could not complete"):
                GoogleEvidenceExecutor(EvidenceStore()).execute(
                    PersistedTask("task-1", "run-1", "collect_evidence", "running", 1, "worker-1", None, None)
                )
        oauth_client.return_value.refresh_access_token.assert_called_once()

    def test_recovery_email_policy_denial_is_failed_not_outcome_unknown(self):
        class RecoveryStore:
            def __init__(self):
                self.updates = []

            def recovery_email_counts(self, **_kwargs):
                return (0, 0)

            def update_action_execution(self, **kwargs):
                self.updates.append(kwargs)

        action = {
            "action_id": "action-1", "run_id": "run-1", "marker": "marker-1",
            "recipient": "outside@example.test", "exception_id": "exception-1",
            "control_code": "unmatched_bank", "remediation": "Provide a statement.",
            "configuration": {"evidence": {"allowed_recipients": ["controller@example.test"]}},
        }
        store = RecoveryStore()
        with self.assertRaisesRegex(TaskBlocked, "violates the approved evidence policy"):
            GmailRecoveryActionExecutor(store)._send(action, object())
        self.assertEqual(store.updates, [{"action_id": "action-1", "status": "failed"}])


if __name__ == "__main__":
    unittest.main()
