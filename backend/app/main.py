"""Authenticated FastAPI API for the US production close workflow."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Protocol
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .connections import ConnectionHealth, ConnectionRegistry, ConnectionStatus
from .domain import CloseService, DeploymentConfig, JournalLine, JournalProposal, PolicyError
from .secrets_store import SecretStoreError, secret_store_from_environment
from .security import create_oauth_transaction, validate_oauth_callback
from .supabase_auth import (
    AuthVerifier,
    AuthenticationError,
    AuthenticationUnavailable,
    SupabaseAuthConfig,
    SupabaseAuthVerifier,
    SupabaseUser,
)
from .supabase_db import (
    PersistedCloseRun,
    SupabaseConfigError,
    SupabaseDatabaseConfig,
    SupabaseWorkflowStore,
    oauth_session_store_from_environment,
)
from .xero_oauth import (
    InMemoryOAuthSessionStore,
    OAuthSessionStore,
    XeroOAuthClient,
    XeroOAuthConfig,
    XeroOAuthError,
)

logger = logging.getLogger("accountingos.api")


def deployment_from_environment() -> DeploymentConfig:
    return DeploymentConfig(
        deployment_id=os.getenv("ACCOUNTINGOS_DEPLOYMENT_ID", "us-production"),
        mode=os.getenv("ACCOUNTINGOS_DEPLOYMENT_MODE", "production"),
        data_class=os.getenv("ACCOUNTINGOS_DATA_CLASS", "live"),
        market=os.getenv("ACCOUNTINGOS_MARKET", "US"),
        currency=os.getenv("ACCOUNTINGOS_CURRENCY", "USD"),
        controller_subject=os.getenv("ACCOUNTINGOS_CONTROLLER_SUBJECT", "unconfigured-controller"),
    )


class WorkflowStore(Protocol):
    def membership_role(self, organization_id: str, issuer: str, subject: str) -> str | None:
        ...

    def organizations_for_user(self, issuer: str, subject: str):
        ...

    def bootstrap_organization(self, **kwargs):
        ...

    def create_close_run(self, **kwargs) -> PersistedCloseRun:
        ...

    def get_close_run(self, run_id: str) -> PersistedCloseRun | None:
        ...

    def connections_for_organization(self, organization_id: str):
        ...

    def upsert_connection(self, **kwargs):
        ...

    def tasks_for_run(self, run_id: str):
        ...

    def events_for_run(self, run_id: str, **kwargs):
        ...

    def retry_run(self, run_id: str) -> PersistedCloseRun:
        ...

    def cancel_run(self, run_id: str) -> PersistedCloseRun:
        ...

    def create_review_package(self, **kwargs):
        ...

    def approve_review_package(self, **kwargs) -> PersistedCloseRun:
        ...


service = CloseService(deployment_from_environment())
connections = ConnectionRegistry(service.deployment)
xero_oauth_client: XeroOAuthClient | None = None
xero_oauth_sessions: OAuthSessionStore = InMemoryOAuthSessionStore()
auth_verifier: AuthVerifier | None = None
workflow_store: WorkflowStore | None = None


def configure_xero_oauth(client: XeroOAuthClient | None) -> None:
    """Inject the server-side Xero client during application bootstrap/tests."""
    global xero_oauth_client
    xero_oauth_client = client


def configure_oauth_sessions(store: OAuthSessionStore) -> None:
    """Inject the OAuth session store during application bootstrap/tests."""
    global xero_oauth_sessions
    xero_oauth_sessions = store


def configure_auth_verifier(verifier: AuthVerifier | None) -> None:
    """Inject a Supabase Auth verifier during startup/tests."""
    global auth_verifier
    auth_verifier = verifier


def configure_workflow_store(store: WorkflowStore | None) -> None:
    """Inject the durable workflow store during startup/tests."""
    global workflow_store
    workflow_store = store


def _build_oauth_session_store() -> OAuthSessionStore:
    try:
        return oauth_session_store_from_environment()
    except SupabaseConfigError as exc:
        logger.info("Durable OAuth session store not configured: %s", exc)
        return InMemoryOAuthSessionStore()


def _build_xero_oauth_client() -> XeroOAuthClient | None:
    try:
        config = XeroOAuthConfig.from_environment()
        secrets = secret_store_from_environment()
    except (XeroOAuthError, SecretStoreError) as exc:
        logger.info("Xero OAuth not configured at startup: %s", exc)
        return None
    return XeroOAuthClient(config, secrets)


def _build_auth_verifier() -> AuthVerifier | None:
    try:
        return SupabaseAuthVerifier(SupabaseAuthConfig.from_environment())
    except AuthenticationUnavailable as exc:
        logger.info("Supabase Auth not configured at startup: %s", exc)
        return None


def _build_workflow_store() -> WorkflowStore | None:
    try:
        return SupabaseWorkflowStore(SupabaseDatabaseConfig.from_environment())
    except SupabaseConfigError as exc:
        logger.info("Durable workflow store not configured at startup: %s", exc)
        return None


@asynccontextmanager
async def lifespan(_: FastAPI):
    if xero_oauth_client is None:
        configure_xero_oauth(_build_xero_oauth_client())
    if os.getenv("SUPABASE_DB_URL", "").strip():
        configure_oauth_sessions(_build_oauth_session_store())
    if auth_verifier is None:
        configure_auth_verifier(_build_auth_verifier())
    if workflow_store is None:
        configure_workflow_store(_build_workflow_store())
    yield


def _cors_origins() -> list[str]:
    raw = os.getenv("ACCOUNTINGOS_CORS_ORIGINS", "http://localhost:3000")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _web_app_url() -> str | None:
    """Return the configured, safe browser return URL for OAuth callbacks."""
    raw = os.getenv("ACCOUNTINGOS_WEB_APP_URL", "").strip().rstrip("/")
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        logger.warning("Ignoring invalid ACCOUNTINGOS_WEB_APP_URL")
        return None
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        logger.warning("Ignoring non-HTTPS ACCOUNTINGOS_WEB_APP_URL")
        return None
    return raw


app = FastAPI(title="AccountingOS API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)


class CreateRunRequest(BaseModel):
    organization_id: str = Field(min_length=1, max_length=200)
    period_start: str
    period_end: str


class BootstrapOrganizationRequest(BaseModel):
    organization_id: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)


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
    package_hash: str


@app.exception_handler(PolicyError)
async def policy_error_handler(_, exc: PolicyError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def authenticated_user(
    authorization: Annotated[str | None, Header()] = None,
) -> SupabaseUser:
    if auth_verifier is None:
        raise HTTPException(status_code=503, detail="Supabase Auth is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token is required")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return auth_verifier.authenticate(token)
    except AuthenticationError as exc:
        code = 503 if isinstance(exc, AuthenticationUnavailable) else 401
        raise HTTPException(status_code=code, detail=str(exc)) from exc


CurrentUser = Annotated[SupabaseUser, Depends(authenticated_user)]


def require_store() -> WorkflowStore:
    if workflow_store is None:
        raise HTTPException(status_code=503, detail="Supabase workflow database is not configured")
    return workflow_store


def require_organization_role(
    organization_id: str,
    user: SupabaseUser,
    allowed: frozenset[str] = frozenset({"controller", "operator", "viewer"}),
) -> str:
    role = require_store().membership_role(organization_id, user.issuer, user.subject)
    if role is None or role not in allowed:
        raise HTTPException(status_code=404, detail="organization was not found")
    return role


def serialize_persisted_run(run: PersistedCloseRun) -> dict[str, object]:
    return {
        "id": run.run_id,
        "organization_id": run.organization_id,
        "period": {"start": run.period_start, "end": run.period_end},
        "status": run.state,
        "deployment": {"mode": run.deployment_mode, "data_class": run.data_class},
        "snapshot_id": run.snapshot_id,
        "package_hash": run.package_hash,
        "actions": [],
    }


def serialize_connection(connection) -> dict[str, object]:
    return {
        "id": connection.connection_id,
        "organization_id": connection.organization_id,
        "provider": connection.provider,
        "provider_environment": connection.provider_environment,
        "provider_tenant_or_account_id": connection.provider_tenant_or_account_id,
        "status": connection.status.value if isinstance(connection.status, ConnectionStatus) else connection.status,
        "granted_scopes": list(connection.granted_scopes),
        "last_verified_at": connection.last_verified_at.isoformat() if connection.last_verified_at else None,
        "last_success_at": connection.last_success_at.isoformat() if connection.last_success_at else None,
        "consent_expires_at": connection.consent_expires_at.isoformat() if connection.consent_expires_at else None,
        "remediation": connection.remediation,
    }


def serialize_task(task) -> dict[str, object]:
    return {
        "id": task.task_id,
        "run_id": task.run_id,
        "key": task.task_key,
        "status": task.state,
        "attempt": task.attempt,
        "last_error": task.last_error,
        "dependencies": list(task.dependencies),
    }


def serialize_task_event(event) -> dict[str, object]:
    return {
        "id": event.event_id,
        "run_id": event.run_id,
        "task_id": event.task_id,
        "type": event.event_type,
        "payload": dict(event.payload),
        "created_at": event.created_at.isoformat(),
    }


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "mode": service.deployment.mode,
        "data_class": service.deployment.data_class,
        "market": service.deployment.market,
        "currency": service.deployment.currency,
        "auth_configured": auth_verifier is not None,
        "database_configured": workflow_store is not None,
    }


@app.get("/api/v1/me")
def get_me(user: CurrentUser) -> dict[str, object]:
    organizations = require_store().organizations_for_user(user.issuer, user.subject)
    return {
        "id": user.subject,
        "email": user.email,
        "organizations": [
            {"id": item.organization_id, "name": item.name, "role": item.role} for item in organizations
        ],
    }


@app.post("/api/v1/organizations/bootstrap", status_code=201)
def bootstrap_organization(request: BootstrapOrganizationRequest, user: CurrentUser) -> dict[str, object]:
    expected_email = os.getenv("ACCOUNTINGOS_BOOTSTRAP_CONTROLLER_EMAIL", "").strip().lower()
    if not expected_email:
        raise HTTPException(status_code=503, detail="bootstrap controller email is not configured")
    if not user.email or user.email.lower() != expected_email:
        raise HTTPException(status_code=403, detail="user is not allowed to bootstrap this organization")
    if (service.deployment.mode, service.deployment.data_class, service.deployment.market, service.deployment.currency) != (
        "production",
        "live",
        "US",
        "USD",
    ):
        raise HTTPException(status_code=409, detail="organization bootstrap is restricted to the US production deployment")
    if require_store().organizations_for_user(user.issuer, user.subject):
        raise HTTPException(status_code=409, detail="bootstrap controller already belongs to an organization")
    organization = require_store().bootstrap_organization(
        organization_id=request.organization_id,
        organization_name=request.name,
        deployment=service.deployment,
        issuer=user.issuer,
        subject=user.subject,
    )
    return {"id": organization.organization_id, "name": organization.name, "role": organization.role}


@app.post("/api/v1/close-runs", status_code=201)
def create_close_run(
    request: CreateRunRequest,
    user: CurrentUser,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    require_organization_role(request.organization_id, user, frozenset({"controller", "operator"}))
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    run = require_store().create_close_run(
        organization_id=request.organization_id,
        deployment=service.deployment,
        period_start=request.period_start,
        period_end=request.period_end,
        idempotency_key=idempotency_key,
    )
    return serialize_persisted_run(run)


@app.get("/api/v1/close-runs/{run_id}")
def get_close_run(run_id: str, user: CurrentUser) -> dict[str, object]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user)
    return serialize_persisted_run(run)


@app.get("/api/v1/close-runs/{run_id}/tasks")
def get_close_run_tasks(run_id: str, user: CurrentUser) -> list[dict[str, object]]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user)
    return [serialize_task(task) for task in require_store().tasks_for_run(run_id)]


@app.get("/api/v1/close-runs/{run_id}/events")
def get_close_run_events(
    run_id: str,
    user: CurrentUser,
    after: int = 0,
    limit: int = 100,
) -> list[dict[str, object]]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user)
    return [
        serialize_task_event(event)
        for event in require_store().events_for_run(run_id, after_event_id=after, limit=limit)
    ]


@app.post("/api/v1/close-runs/{run_id}/retry")
def retry_close_run(run_id: str, user: CurrentUser) -> dict[str, object]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user, frozenset({"controller", "operator"}))
    return serialize_persisted_run(require_store().retry_run(run_id))


@app.post("/api/v1/close-runs/{run_id}/cancel")
def cancel_close_run(run_id: str, user: CurrentUser) -> dict[str, object]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user, frozenset({"controller", "operator"}))
    return serialize_persisted_run(require_store().cancel_run(run_id))


@app.get("/api/v1/organizations/{organization_id}/connections")
def get_connections(organization_id: str, user: CurrentUser) -> list[dict[str, object]]:
    require_organization_role(organization_id, user)
    return [serialize_connection(connection) for connection in require_store().connections_for_organization(organization_id)]


@app.get("/api/v1/organizations/{organization_id}/connections/xero/authorize")
def authorize_xero(organization_id: str, user: CurrentUser) -> dict[str, str]:
    require_organization_role(organization_id, user, frozenset({"controller", "operator"}))
    if service.deployment.mode == "production" and workflow_store is None:
        raise HTTPException(status_code=503, detail="durable workflow storage is required for production OAuth")
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
    if token.scope is not None and frozenset(token.scope.split()) != frozenset(xero_oauth_client.config.scopes):
        raise HTTPException(status_code=502, detail="Xero granted scopes do not match the approved scope profile")
    try:
        registered_tenants = _register_xero_connection(organization_id)
    except (XeroOAuthError, PolicyError) as exc:
        logger.warning("Xero tenant registration failed after OAuth: %s", exc)
        raise HTTPException(status_code=502, detail="Xero authorization could not be linked to an approved tenant") from exc
    if registered_tenants == 0:
        raise HTTPException(status_code=502, detail="Xero did not grant an approved tenant")
    payload = {
        "status": "authorized",
        "organization_id": organization_id,
        "expires_in": token.expires_in,
    }
    web_app_url = _web_app_url()
    if web_app_url:
        return RedirectResponse(
            f"{web_app_url}/?{urlencode({'xero': payload['status'], 'organization_id': organization_id})}",
            status_code=303,
        )
    return payload


def _xero_tenant_allowlist() -> frozenset[str]:
    raw = os.getenv("ACCOUNTINGOS_XERO_TENANT_ALLOWLIST", "")
    return frozenset(
        token for token in raw.replace(",", " ").split() if token and not token.startswith("replace-")
    )


def _register_xero_connection(organization_id: str) -> int:
    if xero_oauth_client is None:
        raise XeroOAuthError("Xero OAuth is not configured")
    allowlist = _xero_tenant_allowlist()
    provider_environment = "demo" if service.deployment.mode == "demo" else "production"
    tenants = xero_oauth_client.list_tenants()
    now = datetime.now(timezone.utc)
    registered = 0
    for tenant in tenants:
        if allowlist and tenant.tenant_id not in allowlist:
            logger.info("Xero tenant %s not in allowlist; skipping registration", tenant.tenant_id)
            continue
        try:
            connection_health = connections.register(
                ConnectionHealth(
                    connection_id=tenant.connection_id or tenant.tenant_id,
                    organization_id=organization_id,
                    provider="xero",
                    provider_environment=provider_environment,
                    provider_tenant_or_account_id=tenant.tenant_id,
                    status=ConnectionStatus.HEALTHY,
                    granted_scopes=xero_oauth_client.config.scopes,
                    last_verified_at=now,
                    last_success_at=now,
                    remediation=None,
                ),
                credential_secret_ref=xero_oauth_client.config.refresh_token_secret_ref,
            )
            if workflow_store is not None:
                workflow_store.upsert_connection(
                    connection_health=connection_health,
                    credential_secret_ref=xero_oauth_client.config.refresh_token_secret_ref,
                )
            registered += 1
        except PolicyError as exc:
            logger.warning("Xero connection registration skipped for tenant %s: %s", tenant.tenant_id, exc)
    return registered


@app.post("/api/v1/close-runs/{run_id}/prepare-review")
def prepare_review(run_id: str, proposals: list[ProposalRequest], user: CurrentUser) -> dict[str, str]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user, frozenset({"controller", "operator"}))
    domain_proposals = tuple(
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
    )
    package = require_store().create_review_package(run_id=run_id, proposals=domain_proposals)
    return {"package_hash": package.package_hash, "status": package.status}


@app.post("/api/v1/close-runs/{run_id}/approvals")
def approve_close_run(run_id: str, request: ApprovalRequest, user: CurrentUser) -> dict[str, object]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user, frozenset({"controller"}))
    return serialize_persisted_run(
        require_store().approve_review_package(
            run_id=run_id,
            package_hash=request.package_hash,
            actor_subject=user.subject,
        )
    )
