import unittest
from datetime import datetime, timedelta, timezone

from app.security import create_oauth_transaction
from app.supabase_db import PostgresOAuthSessionStore, SupabaseConfigError


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
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _row_from_transaction(transaction, organization_id):
    # Mirrors the RETURNING column order of PostgresOAuthSessionStore.consume.
    return (
        transaction.provider,
        transaction.state,
        transaction.code_verifier,
        transaction.code_challenge,
        transaction.redirect_uri,
        transaction.expires_at,
        transaction.oidc,
        transaction.nonce,
        organization_id,
    )


class PostgresOAuthSessionStoreTests(unittest.TestCase):
    def _store(self, connection):
        # Each op opens a "fresh" connection; the factory hands back our fake.
        return PostgresOAuthSessionStore(lambda: connection)

    def test_put_inserts_and_commits_then_closes(self):
        connection = FakeConnection()
        transaction = create_oauth_transaction(
            "xero", "http://localhost:8000/api/v1/connections/xero/callback"
        )
        self._store(connection).put(transaction, "org-1")
        query, params = connection.cursor_instance.executed[0]
        self.assertIn("insert into workflow.oauth_sessions", query)
        self.assertEqual(params[0], transaction.state)
        self.assertEqual(params[2], "org-1")
        self.assertEqual(connection.commits, 1)
        self.assertTrue(connection.closed)

    def test_put_requires_organization(self):
        connection = FakeConnection()
        transaction = create_oauth_transaction(
            "xero", "http://localhost:8000/api/v1/connections/xero/callback"
        )
        with self.assertRaises(SupabaseConfigError):
            self._store(connection).put(transaction, "")

    def test_consume_reconstructs_transaction_and_org(self):
        transaction = create_oauth_transaction(
            "xero", "http://localhost:8000/api/v1/connections/xero/callback"
        )
        connection = FakeConnection(rows=[_row_from_transaction(transaction, "org-9")])
        result = self._store(connection).consume(transaction.state)
        self.assertIsNotNone(result)
        restored, organization_id = result
        self.assertEqual(organization_id, "org-9")
        self.assertEqual(restored.state, transaction.state)
        self.assertEqual(restored.code_verifier, transaction.code_verifier)
        self.assertEqual(restored.redirect_uri, transaction.redirect_uri)
        query, params = connection.cursor_instance.executed[0]
        self.assertIn("delete from workflow.oauth_sessions", query)
        self.assertIn("expires_at > now()", query)  # expired rows are not consumable
        self.assertEqual(params, (transaction.state,))

    def test_consume_returns_none_for_unknown_state(self):
        connection = FakeConnection(rows=[])
        self.assertIsNone(self._store(connection).consume("does-not-exist"))
        self.assertTrue(connection.closed)

    def test_consume_short_circuits_empty_state_without_query(self):
        connection = FakeConnection()
        self.assertIsNone(self._store(connection).consume(""))
        self.assertEqual(connection.cursor_instance.executed, [])

    def test_oidc_transaction_round_trips(self):
        transaction = create_oauth_transaction(
            "xero", "http://localhost:8000/api/v1/connections/xero/callback", oidc=True
        )
        connection = FakeConnection(rows=[_row_from_transaction(transaction, "org-2")])
        restored, _ = self._store(connection).consume(transaction.state)
        self.assertTrue(restored.oidc)
        self.assertEqual(restored.nonce, transaction.nonce)


if __name__ == "__main__":
    unittest.main()
