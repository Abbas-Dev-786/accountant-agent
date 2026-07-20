from datetime import date, datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from app.connections import ConnectionHealth, ConnectionStatus
from app.domain import CloseService, DeploymentConfig
from app.evidence import EvidenceBatch, EvidenceItem, EvidenceScope
from app.supabase_db import (
    PersistedTask,
    SupabaseConfigError,
    SupabaseDatabaseConfig,
    SupabaseRepository,
    SupabaseWorkflowStore,
)


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []
        self.closed = False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows=()):
        self.cursor_instance = FakeCursor(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class SupabaseConfigTests(unittest.TestCase):
    def test_database_url_requires_tls(self):
        config = SupabaseDatabaseConfig("postgresql://postgres:secret@db.example/postgres?sslmode=require")
        self.assertEqual(config.connect_timeout_seconds, 10)
        with self.assertRaises(SupabaseConfigError):
            SupabaseDatabaseConfig("postgresql://postgres:secret@db.example/postgres")
        with self.assertRaises(SupabaseConfigError):
            SupabaseDatabaseConfig(
                "postgresql://postgres:replace-with-password@db.example/postgres?sslmode=require"
            )

    def test_public_service_role_key_is_rejected(self):
        with self.assertRaises(SupabaseConfigError):
            SupabaseDatabaseConfig.from_environment(
                {
                    "SUPABASE_DB_URL": "postgresql://postgres:secret@db.example/postgres?sslmode=require",
                    "NEXT_PUBLIC_SUPABASE_SERVICE_ROLE_KEY": "must-not-exist",
                }
            )


class SupabaseRepositoryTests(unittest.TestCase):
    def test_close_run_is_written_transactionally(self):
        deployment = DeploymentConfig("demo-us", "demo", "synthetic", "US", "USD", "controller")
        run = CloseService(deployment).create_run("org-1", "2026-07-01", "2026-07-31")
        connection = FakeConnection()
        SupabaseRepository(connection).insert_close_run(run)
        self.assertEqual(connection.commits, 1)
        self.assertIn("workflow.close_runs", connection.cursor_instance.executed[0][0])
        self.assertTrue(connection.cursor_instance.closed)

    def test_task_claim_uses_skip_locked_and_lease(self):
        connection = FakeConnection([("task-1", "run-1", "sync", 0)])
        claimed = SupabaseRepository(connection).claim_ready_task("worker-1")
        self.assertEqual(claimed["id"], "task-1")
        self.assertEqual(claimed["attempt"], 1)
        self.assertIn("skip locked", connection.cursor_instance.executed[0][0].lower())
        self.assertEqual(connection.commits, 1)

    def test_migration_keeps_financial_schemas_private_and_rls_enabled(self):
        migration = Path(__file__).parents[1] / ".." / "supabase" / "migrations"
        sql = next(migration.glob("*_us_persistence_foundation.sql")).read_text()
        self.assertIn("create extension if not exists pgcrypto", sql.lower())
        self.assertIn("revoke all on schema workflow", sql.lower())
        self.assertIn("create schema if not exists raw_xero", sql.lower())
        self.assertIn("create schema if not exists raw_bank_us", sql.lower())
        self.assertIn("alter table normalized.source_snapshots enable row level security", sql.lower())
        self.assertIn("create table workflow.tasks", sql.lower())

    def test_workflow_store_upserts_connection_without_token_material(self):
        connection = FakeConnection(
            [
                (
                    "connection-1",
                    "org-1",
                    "xero",
                    "demo",
                    "tenant-1",
                    "healthy",
                    ["offline_access"],
                    datetime(2026, 7, 1, tzinfo=timezone.utc),
                    datetime(2026, 7, 1, tzinfo=timezone.utc),
                    None,
                    None,
                )
            ]
        )
        config = SupabaseDatabaseConfig("postgresql://postgres:secret@db.example/postgres?sslmode=require")
        health = ConnectionHealth(
            "connection-1",
            "org-1",
            "xero",
            "demo",
            "tenant-1",
            ConnectionStatus.HEALTHY,
            ("offline_access",),
        )
        with patch("app.supabase_db.connect", return_value=connection):
            stored = SupabaseWorkflowStore(config).upsert_connection(
                connection_health=health,
                credential_secret_ref="secret://xero/demo/refresh-token",
            )
        self.assertEqual(stored.status, "healthy")
        query, values = connection.cursor_instance.executed[0]
        self.assertIn("on conflict (organization_id, provider, provider_tenant_or_account_id)", query.lower())
        self.assertNotIn("token=", repr(values).lower())

    def test_integrity_migration_guards_workflow_boundaries(self):
        migration = Path(__file__).parents[1] / ".." / "supabase" / "migrations"
        sql = next(migration.glob("*_enforce_workflow_integrity.sql")).read_text().lower()
        self.assertIn("close_runs_organization_request_key_unique", sql)
        self.assertIn("connections_organization_provider_tenant_unique", sql)
        self.assertIn("close_runs_deployment_guard", sql)
        self.assertIn("source_batches_run_guard", sql)
        self.assertIn("raw_xero_context_guard", sql)
        self.assertIn("live close runs require production source batches", sql)

    def test_durable_workflow_migration_keeps_events_and_packages_private(self):
        migration = Path(__file__).parents[1] / ".." / "supabase" / "migrations"
        sql = next(migration.glob("*_durable_workflow_events.sql")).read_text().lower()
        self.assertIn("create table workflow.task_events", sql)
        self.assertIn("create table workflow.review_packages", sql)
        self.assertIn("review_packages_context_guard", sql)
        self.assertIn("revoke all on workflow.task_events", sql)

    def test_close_mapping_migration_versions_configuration_and_binds_runs(self):
        migration = Path(__file__).parents[1] / ".." / "supabase" / "migrations"
        sql = next(migration.glob("*_close_mapping_and_provider_onboarding.sql")).read_text().lower()
        self.assertIn("create table workflow.close_mappings", sql)
        self.assertIn("close_mappings_one_active_per_organization", sql)
        self.assertIn("add column mapping_id", sql)
        self.assertIn("close_runs_mapping_guard", sql)
        self.assertIn("revoke all on workflow.close_mappings", sql)

    def test_durable_close_execution_migration_keeps_outputs_private_and_immutable(self):
        migration = Path(__file__).parents[1] / ".." / "supabase" / "migrations"
        sql = next(migration.glob("*_durable_close_execution.sql")).read_text().lower()
        self.assertIn("create table workflow.reconciliations", sql)
        self.assertIn("create table workflow.reconciliation_exceptions", sql)
        self.assertIn("create table workflow.close_reports", sql)
        self.assertIn("create table workflow.close_artifacts", sql)
        self.assertIn("x-bz-file-retention", (Path(__file__).parents[1] / "app" / "b2.py").read_text().lower())
        self.assertIn("immutable_reconciliations", sql)
        self.assertIn("revoke all on workflow.reconciliations", sql)

    def test_workflow_store_reads_task_dependencies(self):
        connection = FakeConnection(
            [
                (
                    "task-1",
                    "run-1",
                    "synchronize_sources",
                    "pending",
                    0,
                    None,
                    None,
                    None,
                    ["preflight"],
                )
            ]
        )
        config = SupabaseDatabaseConfig("postgresql://postgres:secret@db.example/postgres?sslmode=require")
        with patch("app.supabase_db.connect", return_value=connection):
            tasks = SupabaseWorkflowStore(config).tasks_for_run("run-1")
        self.assertEqual(tasks, (PersistedTask("task-1", "run-1", "synchronize_sources", "pending", 0, None, None, None, ("preflight",)),))

    def test_retry_only_requeues_tasks_whose_dependencies_succeeded(self):
        connection = FakeConnection(
            [
                ("org-1",),
                ("run-1", "org-1", "2026-07-01", "2026-07-31", "synchronizing", "demo", "synthetic", None, None),
            ]
        )
        config = SupabaseDatabaseConfig("postgresql://postgres:secret@db.example/postgres?sslmode=require")
        with patch("app.supabase_db.connect", return_value=connection):
            SupabaseWorkflowStore(config).retry_run("run-1")
        retry_query = connection.cursor_instance.executed[1][0].lower()
        self.assertIn("workflow.task_dependencies", retry_query)
        self.assertIn("prerequisite.state <> 'succeeded'", retry_query)

    def test_evidence_identity_cannot_be_reused_by_another_run(self):
        connection = FakeConnection([("org-1",), ("org-2", "run-2")])
        config = SupabaseDatabaseConfig("postgresql://postgres:secret@db.example/postgres?sslmode=require")
        batch = EvidenceBatch(
            "batch-1",
            EvidenceScope(frozenset({"folder-1"}), "mailbox@example.com", frozenset({"CLOSE"}), date(2026, 7, 1), date(2026, 7, 31)),
            (EvidenceItem("drive:item-1:hash", "drive", "item-1", "hash", datetime(2026, 7, 1, tzinfo=timezone.utc), "document", "folder-1"),),
            datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        with patch("app.supabase_db.connect", return_value=connection):
            with self.assertRaisesRegex(SupabaseConfigError, "already bound"):
                SupabaseWorkflowStore(config).persist_evidence_batch(run_id="run-1", batch=batch)


if __name__ == "__main__":
    unittest.main()
