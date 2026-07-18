"""Versioned demo-scenario and provider-capability verification.

Phase 0 never creates or resets Xero data. It verifies a prepared Demo Company
baseline and records operator-collected evidence for every required demo service.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .domain import PolicyError


REQUIRED_PROVIDER_EVIDENCE = frozenset({"xero", "plaid", "workspace", "b2", "oidc", "openai"})


def _is_placeholder(value: str) -> bool:
    return not value.strip() or "replace-with" in value.lower() or value.lower() in {"todo", "tbd"}


class ScenarioError(PolicyError):
    """Raised when demo scenario or capability evidence cannot prove readiness."""


@dataclass(frozen=True)
class DemoScenario:
    scenario_id: str
    version: int
    period_start: str
    period_end: str
    xero_environment: str
    xero_account_codes: tuple[str, ...]
    xero_fingerprint_environment: str
    xero_tenant_environment: str
    plaid_environment: str
    plaid_test_user: str
    workspace_environment: str

    @classmethod
    def load(cls, path: str | Path) -> "DemoScenario":
        try:
            raw = json.loads(Path(path).read_text())
            deployment = raw["deployment"]
            xero = raw["xero"]
            plaid = raw["plaid"]
            workspace = raw["workspace"]
            period = raw["period"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ScenarioError("scenario manifest is invalid") from exc
        if deployment != {"mode": "demo", "data_class": "synthetic", "market": "US", "currency": "USD"}:
            raise ScenarioError("scenario manifest must describe synthetic US demo data")
        if xero["environment"] != "demo" or plaid["environment"] != "sandbox":
            raise ScenarioError("scenario manifest uses a non-demo provider environment")
        if not xero["required_account_codes"]:
            raise ScenarioError("scenario manifest needs required Xero account codes")
        return cls(
            raw["scenario_id"],
            raw["version"],
            period["start"],
            period["end"],
            xero["environment"],
            tuple(xero["required_account_codes"]),
            xero["baseline_fingerprint_environment"],
            xero["tenant_id_environment"],
            plaid["environment"],
            plaid["test_user"],
            workspace["environment"],
        )


@dataclass(frozen=True)
class XeroBaselineObservation:
    tenant_id: str
    account_codes: tuple[str, ...]
    provider_ids: dict[str, str]

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "tenant_id": self.tenant_id,
                "account_codes": sorted(self.account_codes),
                "provider_ids": dict(sorted(self.provider_ids.items())),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(canonical.encode()).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> "XeroBaselineObservation":
        try:
            raw = json.loads(Path(path).read_text())
            return cls(raw["tenant_id"], tuple(raw["account_codes"]), raw["provider_ids"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ScenarioError("Xero baseline observation is invalid") from exc


@dataclass(frozen=True)
class CapabilityEvidence:
    provider: str
    environment: str
    proven: bool
    captured_at: str
    evidence_ref: str
    request_ids: tuple[str, ...]

    @classmethod
    def load_all(cls, path: str | Path) -> tuple["CapabilityEvidence", ...]:
        try:
            raw = json.loads(Path(path).read_text())
            observations = raw["providers"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ScenarioError("capability evidence is invalid") from exc
        evidence: list[CapabilityEvidence] = []
        for item in observations:
            try:
                datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00"))
                evidence.append(
                    cls(
                        item["provider"],
                        item["environment"],
                        item["proven"],
                        item["captured_at"],
                        item["evidence_ref"],
                        tuple(item["request_ids"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ScenarioError("capability evidence entry is invalid") from exc
        return tuple(evidence)


def verify_xero_baseline(scenario: DemoScenario, observation: XeroBaselineObservation) -> None:
    expected_tenant = os.getenv(scenario.xero_tenant_environment)
    expected_fingerprint = os.getenv(scenario.xero_fingerprint_environment)
    if not expected_tenant or not expected_fingerprint:
        raise ScenarioError("Xero tenant and baseline fingerprint must be configured in the environment")
    if _is_placeholder(expected_tenant) or _is_placeholder(expected_fingerprint):
        raise ScenarioError("Xero tenant and baseline fingerprint cannot use placeholder values")
    if _is_placeholder(observation.tenant_id) or any(
        _is_placeholder(value) for value in observation.provider_ids.values()
    ):
        raise ScenarioError("Xero baseline observation contains placeholder provider identifiers")
    if observation.tenant_id != expected_tenant:
        raise ScenarioError("observed Xero tenant does not match the designated Demo Company")
    if observation.fingerprint != expected_fingerprint:
        raise ScenarioError("observed Xero baseline fingerprint does not match the prepared baseline")
    missing_codes = set(scenario.xero_account_codes).difference(observation.account_codes)
    if missing_codes:
        raise ScenarioError(f"prepared Xero baseline is missing account codes: {', '.join(sorted(missing_codes))}")


def verify_capability_evidence(scenario: DemoScenario, evidence: tuple[CapabilityEvidence, ...]) -> None:
    by_provider = {item.provider: item for item in evidence}
    if len(by_provider) != len(evidence):
        raise ScenarioError("capability evidence contains duplicate provider entries")
    missing = REQUIRED_PROVIDER_EVIDENCE.difference(by_provider)
    if missing:
        raise ScenarioError(f"missing capability evidence: {', '.join(sorted(missing))}")
    expected_environments = {
        "xero": scenario.xero_environment,
        "plaid": scenario.plaid_environment,
        "workspace": scenario.workspace_environment,
        "b2": "demo",
        "oidc": "demo",
        "openai": "demo",
    }
    for provider, environment in expected_environments.items():
        item = by_provider[provider]
        if not item.proven:
            raise ScenarioError(f"{provider} capability is not proven")
        if item.environment != environment:
            raise ScenarioError(f"{provider} evidence is for {item.environment}, not {environment}")
        if not item.evidence_ref or not item.request_ids:
            raise ScenarioError(f"{provider} evidence needs a reference and provider request ID")
        if _is_placeholder(item.evidence_ref) or any(_is_placeholder(value) for value in item.request_ids):
            raise ScenarioError(f"{provider} evidence cannot use placeholders")


def readiness_report(
    scenario: DemoScenario, baseline: XeroBaselineObservation, evidence: tuple[CapabilityEvidence, ...]
) -> dict[str, Any]:
    verify_xero_baseline(scenario, baseline)
    verify_capability_evidence(scenario, evidence)
    return {
        "scenario_id": scenario.scenario_id,
        "scenario_version": scenario.version,
        "period": {"start": scenario.period_start, "end": scenario.period_end},
        "mode": "demo",
        "data_class": "synthetic",
        "xero_baseline_fingerprint": baseline.fingerprint,
        "providers": sorted(item.provider for item in evidence),
        "ready": True,
    }
