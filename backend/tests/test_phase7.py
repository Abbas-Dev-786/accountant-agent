import unittest

from app.domain import DeploymentConfig
from app.expansion import ExpansionRegistry, Market, MarketGateConfig, ReleaseGateError, validate_artifact_market


def us_config(**overrides):
    values = dict(
        market=Market.US,
        deployment=DeploymentConfig("us-prod", "production", "live", "US", "USD", "controller-us"),
        database_id="db-us",
        secret_store_id="secrets-us",
        artifact_bucket_id="b2-us",
        callback_base_url="https://us.example.test",
        provider_tenant_or_partner_id="xero-us",
        xero_production_evidence=True,
        plaid_production_evidence=True,
        fivetran_evidence=True,
    )
    values.update(overrides)
    return MarketGateConfig(**values)


def india_config(**overrides):
    values = dict(
        market=Market.IN,
        deployment=DeploymentConfig("in-prod", "production", "live", "IN", "INR", "controller-in"),
        database_id="db-in",
        secret_store_id="secrets-in",
        artifact_bucket_id="b2-in",
        callback_base_url="https://in.example.test",
        provider_tenant_or_partner_id="setu-in",
        xero_production_evidence=True,
        setu_agreement=True,
        fiu_eligibility=True,
        supported_fip=True,
        retention_policy_approved=True,
    )
    values.update(overrides)
    return MarketGateConfig(**values)


class ExpansionTests(unittest.TestCase):
    def test_us_and_india_register_only_with_distinct_resources(self):
        registry = ExpansionRegistry()
        self.assertTrue(registry.register(us_config()).ready)
        self.assertTrue(registry.register(india_config()).ready)
        self.assertEqual(registry.require_ready(Market.US).deployment.currency, "USD")
        self.assertEqual(registry.require_ready(Market.IN).deployment.currency, "INR")

    def test_missing_india_compliance_gate_blocks_release(self):
        report = ExpansionRegistry().register(india_config(fiu_eligibility=False))
        self.assertFalse(report.ready)
        self.assertTrue(any("gates" in blocker for blocker in report.blockers))

    def test_shared_resources_and_demo_deployment_are_rejected(self):
        registry = ExpansionRegistry()
        self.assertTrue(registry.register(us_config()).ready)
        duplicate = india_config(database_id="db-us")
        self.assertFalse(registry.register(duplicate).ready)
        blocked = ExpansionRegistry().register(
            us_config(deployment=DeploymentConfig("demo", "demo", "synthetic", "US", "USD", "controller"))
        )
        self.assertFalse(blocked.ready)

    def test_artifacts_cannot_cross_market_boundaries(self):
        validate_artifact_market(Market.US, Market.US)
        with self.assertRaises(ReleaseGateError):
            validate_artifact_market(Market.US, Market.IN)


if __name__ == "__main__":
    unittest.main()
