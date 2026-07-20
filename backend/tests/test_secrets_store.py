import unittest

from app.secrets_store import (
    InMemorySecretStore,
    SecretStoreError,
    SupabaseVaultSecretStore,
    secret_store_from_environment,
)
from app.supabase_db import SupabaseDatabaseConfig


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []
        self.closed = False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows=()):
        self.cursor_instance = FakeCursor(rows)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class InMemorySecretStoreTests(unittest.TestCase):
    def test_resolve_and_store_round_trip(self):
        store = InMemorySecretStore({"secret://a": "one"})
        self.assertEqual(store.resolve("secret://a"), "one")
        store.store("secret://a", "two")
        self.assertEqual(store.resolve("secret://a"), "two")

    def test_rejects_non_reference(self):
        store = InMemorySecretStore()
        with self.assertRaises(SecretStoreError):
            store.resolve("plain-value")
        with self.assertRaises(SecretStoreError):
            store.store("plain-value", "x")

    def test_refuses_empty_store(self):
        store = InMemorySecretStore()
        with self.assertRaises(SecretStoreError):
            store.store("secret://a", "")


class SupabaseVaultSecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.config = SupabaseDatabaseConfig("postgresql://postgres:secret@db.example/postgres?sslmode=require")

    def test_resolves_decrypted_secret_and_closes_connection(self):
        connection = FakeConnection([("rotated-token",)])
        store = SupabaseVaultSecretStore(self.config, connection_factory=lambda _: connection)
        self.assertEqual(store.resolve("secret://xero/production/connection-a/refresh-token"), "rotated-token")
        query, values = connection.cursor_instance.executed[0]
        self.assertIn("vault.decrypted_secrets", query.lower())
        self.assertEqual(values, ("secret://xero/production/connection-a/refresh-token",))
        self.assertEqual(connection.commits, 1)
        self.assertTrue(connection.closed)

    def test_store_creates_or_rotates_without_plaintext_workflow_records(self):
        created = FakeConnection([None])
        store = SupabaseVaultSecretStore(self.config, connection_factory=lambda _: created)
        store.store("secret://xero/production/connection-a/refresh-token", "new-token")
        queries = "\n".join(query for query, _ in created.cursor_instance.executed).lower()
        self.assertIn("vault.secrets", queries)
        self.assertIn("vault.create_secret", queries)
        self.assertNotIn("workflow.", queries)

        rotated = FakeConnection([("11111111-1111-1111-1111-111111111111",)])
        SupabaseVaultSecretStore(self.config, connection_factory=lambda _: rotated).store(
            "secret://xero/production/connection-a/refresh-token", "newer-token"
        )
        self.assertIn("vault.update_secret", rotated.cursor_instance.executed[-1][0].lower())
        self.assertTrue(rotated.closed)

    def test_missing_secret_and_invalid_reference_fail_closed(self):
        store = SupabaseVaultSecretStore(self.config, connection_factory=lambda _: FakeConnection([None]))
        with self.assertRaises(SecretStoreError):
            store.resolve("secret://xero/production/missing")
        with self.assertRaises(SecretStoreError):
            store.store("plaintext-token", "value")

    def test_environment_factory_requires_the_private_database_connection(self):
        with self.assertRaisesRegex(SecretStoreError, "SUPABASE_DB_URL"):
            secret_store_from_environment({})
        store = secret_store_from_environment({"SUPABASE_DB_URL": self.config.database_url})
        self.assertIsInstance(store, SupabaseVaultSecretStore)


if __name__ == "__main__":
    unittest.main()
