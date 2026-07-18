from datetime import datetime, timedelta, timezone
import unittest

from app.connections import ConnectionHealth, ConnectionRegistry, ConnectionStatus
from app.domain import DeploymentConfig, PolicyError
from app.security import (
    OIDCConfig,
    SecurityError,
    create_oauth_transaction,
    validate_oauth_callback,
    validate_oidc_claims,
)


class SecurityTests(unittest.TestCase):
    def test_oauth_state_and_pkce_transaction_validates(self) -> None:
        now = datetime(2026, 7, 18, tzinfo=timezone.utc)
        transaction = create_oauth_transaction("xero", "https://demo.example/callback", now=now)
        validate_oauth_callback(transaction, transaction.state, transaction.redirect_uri, now=now)

    def test_oauth_state_mismatch_is_rejected(self) -> None:
        transaction = create_oauth_transaction("xero", "https://demo.example/callback")
        with self.assertRaises(SecurityError):
            validate_oauth_callback(transaction, "wrong", transaction.redirect_uri)

    def test_oidc_nonce_is_required_only_for_oidc_transactions(self) -> None:
        now = datetime(2026, 7, 18, tzinfo=timezone.utc)
        transaction = create_oauth_transaction("oidc", "https://demo.example/callback", now=now, oidc=True)
        with self.assertRaises(SecurityError):
            validate_oauth_callback(transaction, transaction.state, transaction.redirect_uri, now=now)
        validate_oauth_callback(
            transaction,
            transaction.state,
            transaction.redirect_uri,
            now=now,
            returned_nonce=transaction.nonce,
        )

    def test_expired_oidc_claims_are_rejected(self) -> None:
        config = OIDCConfig("https://issuer.example", "accountingos")
        claims = {"iss": config.issuer, "sub": "user-1", "aud": config.audience, "exp": 10}
        with self.assertRaises(SecurityError):
            validate_oidc_claims(claims, config, now=datetime.fromtimestamp(20, timezone.utc))


class ConnectionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.deployment = DeploymentConfig("demo-us", "demo", "synthetic", "US", "USD", "controller-1")
        self.registry = ConnectionRegistry(self.deployment)

    def connection(self, environment: str = "demo") -> ConnectionHealth:
        return ConnectionHealth(
            "connection-1", "org-1", "xero", environment, "demo-tenant", ConnectionStatus.HEALTHY
        )

    def test_demo_rejects_production_connection(self) -> None:
        with self.assertRaises(PolicyError):
            self.registry.register(self.connection("production"), credential_secret_ref="secret://xero/demo")

    def test_demo_rejects_xero_sandbox_identity(self) -> None:
        with self.assertRaises(PolicyError):
            self.registry.register(self.connection("sandbox"), credential_secret_ref="secret://xero/demo")

    def test_secret_reference_cannot_contain_token_material(self) -> None:
        with self.assertRaises(PolicyError):
            self.registry.register(self.connection(), credential_secret_ref="secret://xero/access_token=leaked")

    def test_connection_health_is_scoped_to_organization(self) -> None:
        self.registry.register(self.connection(), credential_secret_ref="secret://xero/demo")
        self.assertEqual(len(self.registry.for_organization("org-1")), 1)
        self.assertEqual(self.registry.for_organization("other-org"), ())


if __name__ == "__main__":
    unittest.main()

