import unittest

from app.production_preflight import production_preflight
from app.supabase_db import SupabaseDatabaseConfig


def production_env():
    return {
        "ACCOUNTINGOS_DEPLOYMENT_ID": "us-production",
        "ACCOUNTINGOS_DEPLOYMENT_MODE": "production",
        "ACCOUNTINGOS_DATA_CLASS": "live",
        "ACCOUNTINGOS_MARKET": "US",
        "ACCOUNTINGOS_CURRENCY": "USD",
        "ACCOUNTINGOS_CORS_ORIGINS": "https://app.example.test",
        "ACCOUNTINGOS_WEB_APP_URL": "https://app.example.test",
        "ACCOUNTINGOS_BOOTSTRAP_CONTROLLER_EMAIL": "controller@example.test",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test-key",
        "SUPABASE_DB_URL": "postgresql://postgres:secret@db.example/postgres?sslmode=require",
        "ACCOUNTINGOS_XERO_CLIENT_ID": "xero-client",
        "ACCOUNTINGOS_XERO_CLIENT_SECRET_REF": "secret://xero/production/client-secret",
        "ACCOUNTINGOS_XERO_REDIRECT_URI": "https://api.example.test/api/v1/connections/xero/callback",
        "ACCOUNTINGOS_XERO_SCOPES": "offline_access accounting.settings.read accounting.invoices.read accounting.banktransactions.read accounting.manualjournals",
        "PLAID_CLIENT_ID": "plaid-client",
        "PLAID_SECRET_REF": "secret://plaid/production/client-secret",
        "PLAID_WEBHOOK_URL": "https://api.example.test/api/v1/webhooks/plaid",
        "GOOGLE_CLIENT_ID": "google-client",
        "GOOGLE_CLIENT_SECRET_REF": "secret://google/production/client-secret",
        "GOOGLE_REDIRECT_URI": "https://api.example.test/api/v1/connections/google/callback",
        "GROQ_API_KEY_REF": "secret://groq/production/api-key",
        "B2_BUCKET_NAME": "accountingos-production",
        "B2_KEY_ID_REF": "secret://b2/production/key-id",
        "B2_APPLICATION_KEY_REF": "secret://b2/production/application-key",
    }


class FakeCursor:
    def __init__(self):
        self.rows = [
            (True, True, True, True, True, True, True, True, True, True),
            ("xero-secret",),
            ("plaid-secret",),
            ("google-secret",),
            ("groq-secret",),
            ("b2-key-id",),
            ("b2-app-key",),
        ]
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
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


class ProductionPreflightTests(unittest.TestCase):
    def test_validates_production_boundary_vault_and_private_schema(self):
        connection = FakeConnection()
        report = production_preflight(production_env(), connection_factory=lambda _: connection)
        self.assertTrue(report.ready)
        self.assertTrue(connection.closed)
        queries = "\n".join(query for query, _ in connection.cursor_instance.executed).lower()
        self.assertIn("set local transaction read only", queries)
        self.assertIn("vault.decrypted_secrets", queries)
        self.assertNotIn("insert", queries)

    def test_rejects_placeholder_and_legacy_file_secret_settings_before_database_access(self):
        env = production_env()
        env["ACCOUNTINGOS_WEB_APP_URL"] = "https://replace-with-web-app-host"
        env["ACCOUNTINGOS_SECRET_STORE_PATH"] = "/private/token-store.json"
        report = production_preflight(env, connection_factory=lambda _: self.fail("database must not be contacted"))
        self.assertFalse(report.ready)
        details = "\n".join(check.detail for check in report.checks)
        self.assertIn("ACCOUNTINGOS_WEB_APP_URL", details)
        self.assertIn("ACCOUNTINGOS_SECRET_STORE_PATH", details)

    def test_reports_all_server_configuration_errors_before_database_access(self):
        env = production_env()
        env["SUPABASE_DB_URL"] = "postgresql://postgres:secret@db.example/postgres"
        env["PLAID_WEBHOOK_URL"] = "https://replace-with-api-host/api/v1/webhooks/plaid"
        env["GROQ_API_KEY_REF"] = "replace-with-groq-key"
        report = production_preflight(env, connection_factory=lambda _: self.fail("database must not be contacted"))
        self.assertFalse(report.ready)
        config_check = next(check for check in report.checks if check.name == "server-side application configuration")
        self.assertIn("Supabase database", config_check.detail)
        self.assertIn("PLAID_WEBHOOK_URL", config_check.detail)
        self.assertIn("GROQ_API_KEY_REF", config_check.detail)

    def test_reports_missing_or_placeholder_vault_secret_without_printing_value(self):
        connection = FakeConnection()
        connection.cursor_instance.rows[-1] = ("replace-with-b2-secret",)
        report = production_preflight(production_env(), connection_factory=lambda _: connection)
        failed = [check for check in report.checks if not check.passed]
        self.assertEqual([(check.name, check.detail) for check in failed], [("Vault B2 application key", "missing or placeholder")])


if __name__ == "__main__":
    unittest.main()
