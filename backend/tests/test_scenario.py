import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.scenario import (
    CapabilityEvidence,
    DemoScenario,
    ScenarioError,
    XeroBaselineObservation,
    readiness_report,
)


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "scenarios" / "demo-scenario-v1.json"


def evidence(provider: str, environment: str) -> CapabilityEvidence:
    return CapabilityEvidence(provider, environment, True, "2026-07-18T00:00:00Z", "ticket/phase-0", ("request-1",))


class ScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = DemoScenario.load(SCENARIO)
        self.baseline = XeroBaselineObservation(
            "demo-tenant", ("200", "610"), {"account-200": "id-1", "account-610": "id-2"}
        )
        self._env_backup = {
            key: os.environ.get(key)
            for key in ("ACCOUNTINGOS_XERO_DEMO_TENANT_ID", "ACCOUNTINGOS_XERO_DEMO_BASELINE_FINGERPRINT")
        }
        os.environ["ACCOUNTINGOS_XERO_DEMO_TENANT_ID"] = self.baseline.tenant_id
        os.environ["ACCOUNTINGOS_XERO_DEMO_BASELINE_FINGERPRINT"] = self.baseline.fingerprint
        self.evidence = (
            evidence("xero", "demo"),
            evidence("plaid", "sandbox"),
            evidence("workspace", "demo"),
            evidence("b2", "demo"),
            evidence("oidc", "demo"),
            evidence("groq", "demo"),
        )

    def tearDown(self) -> None:
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_readiness_report_requires_real_demo_evidence(self) -> None:
        report = readiness_report(self.scenario, self.baseline, self.evidence)
        self.assertTrue(report["ready"])
        self.assertEqual(report["providers"], ["b2", "groq", "oidc", "plaid", "workspace", "xero"])

    def test_xero_baseline_mismatch_blocks_readiness(self) -> None:
        changed_baseline = XeroBaselineObservation(
            "demo-tenant", ("200", "610"), {"account-200": "changed", "account-610": "id-2"}
        )
        with self.assertRaises(ScenarioError):
            readiness_report(self.scenario, changed_baseline, self.evidence)

    def test_missing_provider_evidence_blocks_readiness(self) -> None:
        with self.assertRaises(ScenarioError):
            readiness_report(self.scenario, self.baseline, self.evidence[:-1])

    def test_live_evidence_cannot_prove_demo_readiness(self) -> None:
        invalid = (*self.evidence[:-1], evidence("groq", "production"))
        with self.assertRaises(ScenarioError):
            readiness_report(self.scenario, self.baseline, invalid)

    def test_placeholder_evidence_cannot_prove_readiness(self) -> None:
        placeholder = (*self.evidence[:-1], CapabilityEvidence(
            "groq", "demo", True, "2026-07-18T00:00:00Z", "replace-with-ticket", ("request-1",)
        ))
        with self.assertRaises(ScenarioError):
            readiness_report(self.scenario, self.baseline, placeholder)

    def test_duplicate_provider_evidence_blocks_readiness(self) -> None:
        duplicate = (*self.evidence, evidence("xero", "demo"))
        with self.assertRaises(ScenarioError):
            readiness_report(self.scenario, self.baseline, duplicate)

    def test_manifest_rejects_non_synthetic_deployment(self) -> None:
        raw = json.loads(SCENARIO.read_text())
        raw["deployment"]["data_class"] = "live"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(ScenarioError):
                DemoScenario.load(path)


if __name__ == "__main__":
    unittest.main()
