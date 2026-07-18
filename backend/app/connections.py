"""Connection-health contracts and deployment environment guards."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .domain import DeploymentConfig, PolicyError


class ConnectionStatus(str, Enum):
    CONNECTING = "connecting"
    HEALTHY = "healthy"
    DELAYED = "delayed"
    PARTIAL = "partial"
    EXPIRED = "expired"
    REVOKED = "revoked"
    FAILED = "failed"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class ConnectionHealth:
    connection_id: str
    organization_id: str
    provider: str
    provider_environment: str
    provider_tenant_or_account_id: str
    status: ConnectionStatus
    granted_scopes: tuple[str, ...] = ()
    last_verified_at: datetime | None = None
    last_success_at: datetime | None = None
    consent_expires_at: datetime | None = None
    remediation: str | None = None


@dataclass
class ConnectionRegistry:
    deployment: DeploymentConfig
    _connections: dict[str, ConnectionHealth] = field(default_factory=dict)

    def register(
        self,
        connection: ConnectionHealth,
        *,
        credential_secret_ref: str,
    ) -> ConnectionHealth:
        self._validate_environment(connection)
        if not credential_secret_ref.startswith("secret://"):
            raise PolicyError("connection credentials must be a secret-manager reference")
        if any(token in credential_secret_ref.lower() for token in ("token=", "access_token", "refresh_token")):
            raise PolicyError("connection credential reference must not contain token material")
        self._connections[connection.connection_id] = connection
        return connection

    def for_organization(self, organization_id: str) -> tuple[ConnectionHealth, ...]:
        return tuple(
            connection
            for connection in self._connections.values()
            if connection.organization_id == organization_id
        )

    def _validate_environment(self, connection: ConnectionHealth) -> None:
        if self.deployment.mode == "demo":
            allowed = {"demo", "sandbox"}
        else:
            allowed = {"production"}
        if connection.provider_environment not in allowed:
            raise PolicyError(f"{connection.provider} environment does not match deployment")
        if self.deployment.mode == "demo" and connection.provider == "xero" and connection.provider_environment != "demo":
            raise PolicyError("Xero demo connection must identify the Demo Company environment")

