"""Explicit production-market release gates and deployment separation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .domain import DeploymentConfig, PolicyError


class Market(str, Enum):
    US = "US"
    IN = "IN"


class ReleaseGateError(PolicyError):
    """Raised when a production market has not cleared its external gates."""


@dataclass(frozen=True)
class MarketGateConfig:
    market: Market
    deployment: DeploymentConfig
    database_id: str
    secret_store_id: str
    artifact_bucket_id: str
    callback_base_url: str
    provider_tenant_or_partner_id: str
    xero_production_evidence: bool = False
    plaid_production_evidence: bool = False
    fivetran_evidence: bool = False
    setu_agreement: bool = False
    fiu_eligibility: bool = False
    supported_fip: bool = False
    retention_policy_approved: bool = False

    def validate(self) -> None:
        if self.deployment.mode != "production" or self.deployment.data_class != "live":
            raise ReleaseGateError("live market deployment must be production/live")
        if not all(
            (
                self.database_id,
                self.secret_store_id,
                self.artifact_bucket_id,
                self.callback_base_url,
                self.provider_tenant_or_partner_id,
            )
        ):
            raise ReleaseGateError("live market deployment needs distinct resources and provider identity")
        if self.market == Market.US:
            if self.deployment.market != "US" or self.deployment.currency != "USD":
                raise ReleaseGateError("US release must use the US/USD deployment")
            if not (self.xero_production_evidence and self.plaid_production_evidence and self.fivetran_evidence):
                raise ReleaseGateError("US provider gates are incomplete")
        elif self.market == Market.IN:
            if self.deployment.market != "IN" or self.deployment.currency != "INR":
                raise ReleaseGateError("India release must use the IN/INR deployment")
            if not (self.xero_production_evidence and self.setu_agreement and self.fiu_eligibility and self.supported_fip and self.retention_policy_approved):
                raise ReleaseGateError("India provider, compliance, or retention gates are incomplete")
        else:
            raise ReleaseGateError("unsupported live market")


@dataclass(frozen=True)
class ReleaseReport:
    market: Market
    ready: bool
    blockers: tuple[str, ...]


class ExpansionRegistry:
    def __init__(self) -> None:
        self.deployments: dict[Market, MarketGateConfig] = {}

    def register(self, config: MarketGateConfig) -> ReleaseReport:
        blockers: list[str] = []
        try:
            config.validate()
        except ReleaseGateError as exc:
            blockers.append(str(exc))
        for existing in self.deployments.values():
            for field in ("database_id", "secret_store_id", "artifact_bucket_id", "callback_base_url", "provider_tenant_or_partner_id"):
                if getattr(existing, field) == getattr(config, field):
                    blockers.append(f"{field} must not be shared across markets")
        if blockers:
            return ReleaseReport(config.market, False, tuple(dict.fromkeys(blockers)))
        self.deployments[config.market] = config
        return ReleaseReport(config.market, True, ())

    def require_ready(self, market: Market) -> MarketGateConfig:
        config = self.deployments.get(market)
        if config is None:
            raise ReleaseGateError(f"{market.value} market is not registered")
        config.validate()
        return config


def validate_artifact_market(market: Market, artifact_market: Market) -> None:
    if market != artifact_market:
        raise ReleaseGateError("artifacts cannot cross market deployment boundaries")
