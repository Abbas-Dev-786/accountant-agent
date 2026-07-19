"""FastAPI shell for the AccountingOS foundation."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .domain import CloseService, DeploymentConfig, JournalLine, JournalProposal, PolicyError
from .connections import ConnectionHealth, ConnectionRegistry, ConnectionStatus
from .security import create_oauth_transaction, validate_oauth_callback
from .secrets_store import SecretStoreError, secret_store_from_environment
from .xero_oauth import (
    InMemoryOAuthSessionStore,
    XeroOAuthClient,
    XeroOAuthConfig,
    XeroOAuthError,
)

logger = logging.getLogger("accountingos.api")


def deployment_from_environment() -> DeploymentConfig:
    return DeploymentConfig(
        deployment_id=os.getenv("ACCOUNTINGOS_DEPLOYMENT_ID", "demo-us"),
        mode=os.getenv("ACCOUNTINGOS_DEPLOYMENT_MODE", "demo"),
        data_class=os.getenv("ACCOUNTINGOS_DATA_CLASS", "synthetic"),
        market=os.getenv("ACCOUNTINGOS_MARKET", "US"),
        currency=os.getenv("ACCOUNTINGOS_CURRENCY", "USD"),
        controller_subject=os.getenv("ACCOUNTINGOS_CONTROLLER_SUBJECT", "demo-controller"),
    )


service = CloseService(deployment_from_environment())
connections = ConnectionRegistry(service.deployment)
xero_oauth_client: XeroOAuthClient | None = None
xero_oauth_sessions = InMemoryOAuthSessionStore()


def configure_xero_oauth(client: XeroOAuthClient | None) -> None:
    """Inject the server-side Xero client during application bootstrap/tests."""
    global xero_oauth_client
    xero_oauth_client = client


def _build_xero_oauth_client() -> XeroOAuthClient | None:
    """Construct the Xero client from the environment, or None if unconfigured.

    Missing/placeholder config is a normal state for the pure-domain demo, so a
    configuration error is logged and swallowed rather than aborting startup.
    """
    try:
        config = XeroOAuthConfig.from_environment()
        secrets = secret_store_from_environment()
    except (XeroOAuthError, SecretStoreError) as exc:
        logger.info("Xero OAuth not configured at startup: %s", exc)
        return None
    return XeroOAuthClient(config, secrets)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Only auto-wire when nothing was injected (tests inject their own client).
    if xero_oauth_client is None:
        configure_xero_oauth(_build_xero_oauth_client())
    yield


app = FastAPI(title="AccountingOS API", version="0.1.0", lifespan=lifespan)


class CreateRunRequest(BaseModel):
    organization_id: str = Field(min_length=1)
    period_start: str
    period_end: str


class JournalLineRequest(BaseModel):
    account_code: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    evidence_ids: list[str]


class ProposalRequest(BaseModel):
    proposal_id: str
    journal_date: str
    narration: str
    lines: list[JournalLineRequest]


class ApprovalRequest(BaseModel):
    actor_subject: str
    package_hash: str


@app.exception_handler(PolicyError)
async def policy_error_handler(_, exc: PolicyError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def serialize_run(run) -> dict[str, object]:
    return {
        "id": run.run_id,
        "organization_id": run.organization_id,
        "period": {"start": run.period_start, "end": run.period_end},
        "status": run.state.value,
        "deployment": {"mode": run.deployment.mode, "data_class": run.deployment.data_class},
        "snapshot_id": run.snapshot.snapshot_id if run.snapshot else None,
        "package_hash": run.package_hash,
        "actions": [
            {
                "id": action.action_id,
                "proposal_id": action.proposal_id,
                "status": action.status.value,
                "xero_journal_id": action.xero_journal_id,
            }
            for action in run.actions.values()
        ],
    }


def get_run(run_id: str):
    run = service.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    return run


def serialize_connection(connection) -> dict[str, object]:
    return {
        "id": connection.connection_id,
        "organization_id": connection.organization_id,
        "provider": connection.provider,
        "provider_environment": connection.provider_environment,
        "provider_tenant_or_account_id": connection.provider_tenant_or_account_id,
        "status": connection.status.value,
        "granted_scopes": list(connection.granted_scopes),
        "last_verified_at": connection.last_verified_at.isoformat() if connection.last_verified_at else None,
        "last_success_at": connection.last_success_at.isoformat() if connection.last_success_at else None,
        "consent_expires_at": connection.consent_expires_at.isoformat() if connection.consent_expires_at else None,
        "remediation": connection.remediation,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": service.deployment.mode, "data_class": service.deployment.data_class}


@app.post("/api/v1/close-runs", status_code=201)
def create_close_run(request: CreateRunRequest) -> dict[str, str]:
    run = service.create_run(request.organization_id, request.period_start, request.period_end)
    service.begin_sync(run)
    return {"id": run.run_id, "status": run.state.value, "data_class": run.deployment.data_class}


@app.get("/api/v1/close-runs/{run_id}")
def get_close_run(run_id: str) -> dict[str, object]:
    return serialize_run(get_run(run_id))


@app.get("/api/v1/organizations/{organization_id}/connections")
def get_connections(organization_id: str) -> list[dict[str, object]]:
    return [serialize_connection(connection) for connection in connections.for_organization(organization_id)]


@app.get("/api/v1/organizations/{organization_id}/connections/xero/authorize")
def authorize_xero(organization_id: str) -> dict[str, str]:
    if xero_oauth_client is None:
        raise HTTPException(status_code=503, detail="Xero OAuth is not configured")
    transaction = create_oauth_transaction("xero", xero_oauth_client.config.redirect_uri)
    xero_oauth_sessions.put(transaction, organization_id)
    try:
        authorization_url = xero_oauth_client.authorization_url(transaction.state, transaction.code_challenge)
    except XeroOAuthError as exc:
        raise HTTPException(status_code=503, detail="Xero OAuth configuration is invalid") from exc
    return {"authorization_url": authorization_url, "state": transaction.state}


@app.get("/api/v1/connections/xero/callback")
def xero_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    if xero_oauth_client is None:
        raise HTTPException(status_code=503, detail="Xero OAuth is not configured")
    if not state:
        raise HTTPException(status_code=400, detail="Xero OAuth state is required")
    session = xero_oauth_sessions.consume(state)
    if session is None:
        raise HTTPException(status_code=400, detail="Xero OAuth state is invalid or already used")
    transaction, organization_id = session
    if error:
        raise HTTPException(status_code=400, detail="Xero authorization was declined")
    if not code:
        raise HTTPException(status_code=400, detail="Xero OAuth authorization code is required")
    try:
        validate_oauth_callback(transaction, state, xero_oauth_client.config.redirect_uri)
        token = xero_oauth_client.exchange_code(code, transaction.code_verifier)
    except XeroOAuthError as exc:
        raise HTTPException(status_code=502, detail="Xero OAuth token exchange failed") from exc
    except PolicyError as exc:
        raise HTTPException(status_code=400, detail="Xero OAuth callback validation failed") from exc
    _register_xero_connection(organization_id)
    return {"status": "authorized", "organization_id": organization_id, "expires_in": token.expires_in}


def _register_xero_connection(organization_id: str) -> None:
    """Discover the connected tenant and register a connection (best-effort).

    Gated on a configured demo tenant id so the pure-domain demo (which has none)
    performs no network call. Any discovery/registration failure is logged and
    swallowed: it must never change the OAuth callback outcome, since the tokens
    were already exchanged and persisted.
    """
    if xero_oauth_client is None:
        return
    expected_tenant = os.getenv("ACCOUNTINGOS_XERO_DEMO_TENANT_ID", "")
    if not expected_tenant or expected_tenant.startswith("replace-"):
        return
    try:
        tenants = xero_oauth_client.list_tenants()
        match = next((t for t in tenants if t.tenant_id == expected_tenant), None)
        if match is None:
            logger.warning("configured Xero demo tenant not present in connections response")
            return
        now = datetime.now(timezone.utc)
        connections.register(
            ConnectionHealth(
                connection_id=match.connection_id or match.tenant_id,
                organization_id=organization_id,
                provider="xero",
                provider_environment="demo",
                provider_tenant_or_account_id=match.tenant_id,
                status=ConnectionStatus.HEALTHY,
                granted_scopes=xero_oauth_client.config.scopes,
                last_verified_at=now,
                last_success_at=now,
            ),
            credential_secret_ref=xero_oauth_client.config.refresh_token_secret_ref,
        )
    except (XeroOAuthError, PolicyError) as exc:
        logger.warning("Xero connection registration skipped: %s", exc)


@app.post("/api/v1/close-runs/{run_id}/prepare-review")
def prepare_review(run_id: str, proposals: list[ProposalRequest]) -> dict[str, str]:
    run = get_run(run_id)
    try:
        domain_proposals = [
            JournalProposal(
                item.proposal_id,
                item.journal_date,
                item.narration,
                tuple(
                    JournalLine(line.account_code, line.debit, line.credit, tuple(line.evidence_ids))
                    for line in item.lines
                ),
            )
            for item in proposals
        ]
        package_hash = service.prepare_for_review(run, domain_proposals)
    except PolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"package_hash": package_hash, "status": run.state.value}


@app.post("/api/v1/close-runs/{run_id}/approvals")
def approve_close_run(run_id: str, request: ApprovalRequest) -> dict[str, object]:
    run = get_run(run_id)
    try:
        service.approve(run, request.actor_subject, request.package_hash)
    except PolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return serialize_run(run)
