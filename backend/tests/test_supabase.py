from datetime import datetime, timezone
from pathlib import Path
import unittest

from app.domain import CloseService, DeploymentConfig
from app.supabase_db import SupabaseConfigError, SupabaseDatabaseConfig, SupabaseRepository


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
        self.assertIn("alter table normalized.source_snapshots enable row level security", sql.lower())
        self.assertIn("create table workflow.tasks", sql.lower())


if __name__ == "__main__":
    unittest.main()
