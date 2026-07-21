"""Authenticated FastAPI API for the US production close workflow."""

from __future__ import annotations

import logging
import os
import asyncio
import json
import threading
from hashlib import sha256
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Protocol
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .close_mapping import (
    BankLedgerMapping,
    CloseMappingDraft,
    EvidenceChecklistRequirement,
    EvidenceConfiguration,
    MatchingRules,
)
from .connections import ConnectionHealth, ConnectionRegistry, ConnectionStatus
from .domain import CloseService, DeploymentConfig, JournalLine, JournalProposal, PolicyError
from .google_oauth import GoogleOAuthClient, GoogleOAuthConfig, GoogleOAuthError
from .plaid_link import PlaidLinkClient, PlaidLinkConfig, PlaidLinkError
from .plaid_webhooks import PlaidWebhookError, PlaidWebhookVerifier
from .provider_runtime import UrllibJsonTransport
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
    SupabaseConnectionUnavailable,
    SupabaseConfigError,
    SupabaseDatabaseConfig,
    SupabaseWorkflowStore,
    oauth_session_store_from_environment,
)
from .reports import build_journal_proposal
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

    def ensure_initial_organization(self, **kwargs):
        ...

    def create_close_run(self, **kwargs) -> PersistedCloseRun:
        ...

    def get_close_run(self, run_id: str) -> PersistedCloseRun | None:
        ...

    def close_runs_for_organization(self, organization_id: str, **kwargs):
        ...

    def connections_for_organization(self, organization_id: str):
        ...

    def upsert_connection(self, **kwargs):
        ...

    def disconnect_connection(self, **kwargs) -> str | None:
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

    def request_review_changes(self, **kwargs) -> PersistedCloseRun:
        ...

    def controller_subject_for_organization(self, organization_id: str) -> str | None:
        ...

    def active_close_mapping(self, organization_id: str):
        ...

    def save_close_mapping(self, **kwargs):
        ...

    def review_data_for_run(self, run_id: str):
        ...

    def resolve_reconciliation_exception(self, **kwargs):
        ...

    def queue_exception_recovery_email(self, **kwargs):
        ...

    def record_webhook_receipt(self, **kwargs) -> bool:
        ...


service = CloseService(deployment_from_environment())
connections = ConnectionRegistry(service.deployment)
xero_oauth_client: XeroOAuthClient | None = None
google_oauth_client: GoogleOAuthClient | None = None
plaid_link_client: PlaidLinkClient | None = None
xero_oauth_sessions: OAuthSessionStore = InMemoryOAuthSessionStore()
auth_verifier: AuthVerifier | None = None
workflow_store: WorkflowStore | None = None
plaid_webhook_verifier: PlaidWebhookVerifier | None = None
_plaid_webhook_verifier_lock = threading.Lock()
_MAX_PLAID_WEBHOOK_BYTES = 1_048_576


def configure_xero_oauth(client: XeroOAuthClient | None) -> None:
    """Inject the server-side Xero client during application bootstrap/tests."""
    global xero_oauth_client
    xero_oauth_client = client


def configure_google_oauth(client: GoogleOAuthClient | None) -> None:
    """Inject the server-side Google Workspace OAuth client for startup/tests."""
    global google_oauth_client
    google_oauth_client = client


def configure_plaid_link(client: PlaidLinkClient | None) -> None:
    """Inject the server-side Plaid Link client for startup/tests."""
    global plaid_link_client
    plaid_link_client = client


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


def configure_plaid_webhook_verifier(verifier: PlaidWebhookVerifier | None) -> None:
    """Inject the shared Plaid verifier for startup/tests."""
    global plaid_webhook_verifier
    plaid_webhook_verifier = verifier


def require_plaid_webhook_verifier() -> PlaidWebhookVerifier:
    global plaid_webhook_verifier
    if plaid_webhook_verifier is not None:
        return plaid_webhook_verifier
    with _plaid_webhook_verifier_lock:
        if plaid_webhook_verifier is None:
            config = PlaidLinkConfig.from_environment()
            plaid_webhook_verifier = PlaidWebhookVerifier(
                client_id=config.client_id,
                client_secret_ref=config.client_secret_ref,
                secret_resolver=secret_store_from_environment(),
                transport=UrllibJsonTransport(),
            )
    return plaid_webhook_verifier


async def read_limited_request_body(request: Request, *, maximum_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > maximum_bytes:
                raise HTTPException(status_code=413, detail="Plaid webhook payload is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Plaid webhook Content-Length is invalid") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum_bytes:
            raise HTTPException(status_code=413, detail="Plaid webhook payload is too large")
        chunks.append(chunk)
    return b"".join(chunks)


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


def _build_google_oauth_client() -> GoogleOAuthClient | None:
    try:
        config = GoogleOAuthConfig.from_environment()
        secrets = secret_store_from_environment()
    except (GoogleOAuthError, SecretStoreError) as exc:
        logger.info("Google OAuth not configured at startup: %s", exc)
        return None
    return GoogleOAuthClient(config, secrets)


def _build_plaid_link_client() -> PlaidLinkClient | None:
    try:
        config = PlaidLinkConfig.from_environment()
        secrets = secret_store_from_environment()
    except (PlaidLinkError, SecretStoreError) as exc:
        logger.info("Plaid Link not configured at startup: %s", exc)
        return None
    return PlaidLinkClient(config, secrets)


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
    if google_oauth_client is None:
        configure_google_oauth(_build_google_oauth_client())
    if plaid_link_client is None:
        configure_plaid_link(_build_plaid_link_client())
    if os.getenv("SUPABASE_DB_URL", "").strip():
        configure_oauth_sessions(_build_oauth_session_store())
    if auth_verifier is None:
        configure_auth_verifier(_build_auth_verifier())
    if workflow_store is None:
        configure_workflow_store(_build_workflow_store())
    yield


def _cors_origins() -> list[str]:
    raw = os.getenv("ACCOUNTINGOS_CORS_ORIGINS", "http://localhost:3000")
    origins = list(dict.fromkeys(origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()))

    # localhost and 127.0.0.1 are different browser origins, even though both
    # reach the same local process. Keep local API startup resilient to either
    # spelling without broadening the production allow-list.
    for origin in tuple(origins):
        parsed = urlparse(origin)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
            continue
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            logger.warning("Ignoring malformed CORS origin: %s", origin)
            continue
        alternate_host = "127.0.0.1" if parsed.hostname == "localhost" else "localhost"
        alternate_origin = f"http://{alternate_host}{port}"
        if alternate_origin not in origins:
            origins.append(alternate_origin)
    return origins


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


def _oauth_callback_error(provider: str, status_code: int, organization_id: str | None = None):
    """Return users to the controller UI without exposing a raw API error page."""
    web_app_url = _web_app_url()
    if web_app_url:
        query = {provider: "error"}
        if organization_id:
            query["organization_id"] = organization_id
        return RedirectResponse(f"{web_app_url}/?{urlencode(query)}", status_code=303)
    return JSONResponse(status_code=status_code, content={"detail": f"{provider} authorization could not be completed"})


def _discard_xero_refresh_token(refresh_token_secret_ref: str) -> None:
    """Compensate for a post-exchange registration failure without retaining a live credential."""
    if xero_oauth_client is None:
        return
    try:
        xero_oauth_client.secrets.delete(refresh_token_secret_ref)
    except Exception:
        # The user must still receive the generic OAuth failure response.  Do
        # not include a Vault/provider error in that redirect, but make an
        # incomplete compensation visible to operators.
        logger.exception("Xero OAuth refresh-token cleanup failed after registration failure")


app = FastAPI(title="AccountingOS API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)


@app.exception_handler(SupabaseConnectionUnavailable)
async def supabase_connection_unavailable(_: Request, exc: SupabaseConnectionUnavailable) -> JSONResponse:
    """Return a retryable API response when Postgres cannot be reached."""
    logger.warning("Supabase Postgres is unavailable: %s", exc, exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "The workflow database is temporarily unavailable. Please try again shortly."},
    )


class CreateRunRequest(BaseModel):
    organization_id: str = Field(min_length=1, max_length=200)
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
    package_hash: str
    decision: str = Field(default="approved", pattern=r"^(approved|changes_requested)$")
    comment: str | None = Field(default=None, max_length=2000)


class BankMappingRequest(BaseModel):
    plaid_account_id: str = Field(min_length=1, max_length=300)
    xero_account_code: str = Field(min_length=1, max_length=80)
    xero_account_name: str = Field(min_length=1, max_length=300)


class MatchingRulesRequest(BaseModel):
    date_window_days: int = Field(ge=0, le=60)
    fee_tolerance: Decimal = Field(ge=0)
    materiality_threshold: Decimal = Field(ge=0)
    pending_policy: str = Field(pattern=r"^(exclude|exception)$")
    max_aggregate_size: int = Field(ge=1, le=100)


class EvidenceChecklistRequirementRequest(BaseModel):
    requirement_id: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    required_tags: list[str] = Field(default_factory=list, max_length=20)
    allowed_kinds: list[str] = Field(default_factory=lambda: ["document", "email"], min_length=1, max_length=3)


class EvidenceChecklistRequest(BaseModel):
    id: str = Field(default="close-evidence-v1", min_length=1, max_length=120)
    version: int = Field(default=1, ge=1, le=10_000)
    requirements: list[EvidenceChecklistRequirementRequest] = Field(default_factory=list, max_length=50)


class EvidenceConfigurationRequest(BaseModel):
    drive_folder_ids: list[str] = Field(min_length=1, max_length=50)
    gmail_mailbox: str = Field(min_length=3, max_length=300)
    gmail_labels: list[str] = Field(min_length=1, max_length=50)
    allowed_recipients: list[str] = Field(min_length=1, max_length=100)
    retention_policy_version: str = Field(min_length=1, max_length=100)
    checklist: EvidenceChecklistRequest = Field(default_factory=EvidenceChecklistRequest)


class CloseMappingRequest(BaseModel):
    xero_tenant_id: str = Field(min_length=1, max_length=300)
    bank_mappings: list[BankMappingRequest] = Field(min_length=1, max_length=50)
    matching_rules: MatchingRulesRequest
    permitted_journal_account_codes: list[str] = Field(min_length=1, max_length=200)
    evidence: EvidenceConfigurationRequest
    journal_adjustment_account_code: str | None = Field(default=None, min_length=1, max_length=80)

    def to_domain(self) -> CloseMappingDraft:
        return CloseMappingDraft(
            self.xero_tenant_id,
            tuple(
                BankLedgerMapping(item.plaid_account_id, item.xero_account_code, item.xero_account_name)
                for item in self.bank_mappings
            ),
            MatchingRules(
                self.matching_rules.date_window_days,
                self.matching_rules.fee_tolerance,
                self.matching_rules.materiality_threshold,
                self.matching_rules.pending_policy,
                self.matching_rules.max_aggregate_size,
            ),
            tuple(self.permitted_journal_account_codes),
            EvidenceConfiguration(
                tuple(self.evidence.drive_folder_ids),
                self.evidence.gmail_mailbox,
                tuple(self.evidence.gmail_labels),
                tuple(self.evidence.allowed_recipients),
                self.evidence.retention_policy_version,
                self.evidence.checklist.id,
                self.evidence.checklist.version,
                tuple(
                    EvidenceChecklistRequirement(
                        item.requirement_id,
                        item.description,
                        tuple(item.required_tags),
                        tuple(item.allowed_kinds),
                    )
                    for item in self.evidence.checklist.requirements
                ),
            ),
            self.journal_adjustment_account_code,
        )


class PlaidExchangeRequest(BaseModel):
    public_token: str = Field(min_length=1, max_length=2000)
    selected_account_ids: list[str] = Field(min_length=1, max_length=50)


class ExceptionResolutionRequest(BaseModel):
    status: str = Field(pattern=r"^(resolved|ignored)$")
    comment: str = Field(min_length=3, max_length=2000)


class RecoveryEmailRequest(BaseModel):
    recipient: str = Field(min_length=3, max_length=300)


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


def require_configured_controller(organization_id: str, user: SupabaseUser) -> None:
    require_organization_role(organization_id, user, frozenset({"controller"}))
    configured = require_store().controller_subject_for_organization(organization_id)
    if not configured or configured != user.subject:
        raise HTTPException(status_code=403, detail="only the configured controller may decide this close package")


def serialize_persisted_run(run: PersistedCloseRun, *, actions: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "id": run.run_id,
        "organization_id": run.organization_id,
        "period": {"start": run.period_start, "end": run.period_end},
        "status": run.state,
        "deployment": {"mode": run.deployment_mode, "data_class": run.data_class},
        "snapshot_id": run.snapshot_id,
        "package_hash": run.package_hash,
        "actions": actions or [],
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


def serialize_close_mapping(mapping) -> dict[str, object] | None:
    if mapping is None:
        return None
    return {
        "id": mapping.mapping_id,
        "organization_id": mapping.organization_id,
        "version": mapping.version,
        "status": mapping.status,
        "configuration": dict(mapping.configuration),
        "approved_by_subject": mapping.approved_by_subject,
        "created_at": mapping.created_at.isoformat() if getattr(mapping, "created_at", None) else None,
    }


def _connection_secret_ref(provider: str, organization_id: str, connection_id: str, credential_kind: str) -> str:
    """Create an opaque secret-manager reference without leaking external IDs."""
    digest = sha256(f"{organization_id}|{connection_id}".encode("utf-8")).hexdigest()[:32]
    return f"secret://{provider}/production/connection-{digest}/{credential_kind}"


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
    if not organizations:
        if (service.deployment.mode, service.deployment.data_class, service.deployment.market, service.deployment.currency) != (
            "production",
            "live",
            "US",
            "USD",
        ):
            raise HTTPException(status_code=409, detail="automatic organization provisioning requires the US production deployment")
        organization = require_store().ensure_initial_organization(
            deployment=service.deployment,
            issuer=user.issuer,
            subject=user.subject,
        )
        if organization is None:
            raise HTTPException(status_code=403, detail="this workspace is already initialized; ask its controller for access")
        organizations = (organization,)
    return {
        "id": user.subject,
        "email": user.email,
        "organizations": [
            {"id": item.organization_id, "name": item.name, "role": item.role} for item in organizations
        ],
    }

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
    review_data = getattr(require_store(), "review_data_for_run", None)
    actions = []
    if callable(review_data):
        review = review_data(run_id)
        actions = [dict(item) for item in review.actions]
    return serialize_persisted_run(run, actions=actions)


@app.get("/api/v1/organizations/{organization_id}/close-runs")
def get_organization_close_runs(
    organization_id: str,
    user: CurrentUser,
    limit: int = 50,
) -> list[dict[str, object]]:
    require_organization_role(organization_id, user)
    return [
        serialize_persisted_run(run)
        for run in require_store().close_runs_for_organization(organization_id, limit=limit)
    ]


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


@app.get("/api/v1/close-runs/{run_id}/events/stream")
async def stream_close_run_events(
    run_id: str,
    user: CurrentUser,
    after: int = 0,
) -> StreamingResponse:
    """Authenticated SSE with durable replay from the supplied event cursor.

    The browser sends its bearer token using fetch rather than an EventSource
    query string, so the session is never placed in a URL or event log.
    """
    store = require_store()
    run = await asyncio.to_thread(store.get_close_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    await asyncio.to_thread(require_organization_role, run.organization_id, user)
    if after < 0:
        raise HTTPException(status_code=422, detail="event cursor must be non-negative")

    terminal_states = frozenset({"blocked", "failed", "cancelled", "awaiting_approval", "changes_requested", "approved", "action_failed"})
    async def event_stream():
        cursor = after
        idle_cycles = 0
        while True:
            batch = await asyncio.to_thread(store.events_for_run, run_id, after_event_id=cursor, limit=100)
            if batch:
                idle_cycles = 0
                for event in batch:
                    cursor = event.event_id
                    payload = serialize_task_event(event)
                    yield f"id: {event.event_id}\nevent: close_progress\ndata: {json.dumps(payload, default=str)}\n\n"
            else:
                idle_cycles += 1
                current_run = await asyncio.to_thread(store.get_close_run, run_id)
                if current_run is None or current_run.state in terminal_states:
                    # A worker can commit its terminal state between the empty
                    # event read and this state read. Replay once more before
                    # closing the stream so that final task events are never
                    # silently lost.
                    final_batch = await asyncio.to_thread(store.events_for_run, run_id, after_event_id=cursor, limit=100)
                    if final_batch:
                        for event in final_batch:
                            cursor = event.event_id
                            payload = serialize_task_event(event)
                            yield f"id: {event.event_id}\nevent: close_progress\ndata: {json.dumps(payload, default=str)}\n\n"
                        continue
                    return
                # Send at least one frame every five seconds. Many reverse
                # proxies drop quiet SSE responses after roughly one minute.
                yield ": keepalive\n\n"
                await asyncio.sleep(min(5, 0.5 * (1.5 ** min(idle_cycles, 5))))

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/close-runs/{run_id}/review")
def get_close_run_review(run_id: str, user: CurrentUser) -> dict[str, object]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user)
    review = require_store().review_data_for_run(run_id)
    return {
        "run_id": review.run_id,
        "snapshot_id": review.snapshot_id,
        "mapping": serialize_close_mapping(review.mapping),
        "source_batches": [dict(item) for item in review.source_batches],
        "evidence_items": [dict(item) for item in review.evidence_items],
        "evidence_checklist": dict(review.evidence_checklist) if review.evidence_checklist else None,
        "review_package": dict(review.review_package) if review.review_package else None,
        "journal_proposals": [dict(item) for item in review.journal_proposals],
        "reconciliation_matches": [dict(item) for item in review.reconciliation_matches],
        "reconciliation_exceptions": [dict(item) for item in review.reconciliation_exceptions],
        "report": dict(review.report) if review.report else None,
        "artifacts": [dict(item) for item in review.artifacts],
        "actions": [dict(item) for item in review.actions],
    }


@app.post("/api/v1/close-runs/{run_id}/exceptions/{exception_id}/resolve")
def resolve_reconciliation_exception(
    run_id: str,
    exception_id: str,
    request: ExceptionResolutionRequest,
    user: CurrentUser,
) -> dict[str, str]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user, frozenset({"controller", "operator"}))
    require_store().resolve_reconciliation_exception(
        run_id=run_id, exception_id=exception_id, status=request.status,
        comment=request.comment, actor_subject=user.subject,
    )
    return {"status": request.status}


@app.post("/api/v1/close-runs/{run_id}/exceptions/{exception_id}/recovery-email", status_code=202)
def queue_recovery_email(
    run_id: str,
    exception_id: str,
    request: RecoveryEmailRequest,
    user: CurrentUser,
) -> dict[str, object]:
    run = require_store().get_close_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    require_organization_role(run.organization_id, user, frozenset({"controller", "operator"}))
    return dict(require_store().queue_exception_recovery_email(
        run_id=run_id, exception_id=exception_id, recipient=request.recipient,
    ))


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


@app.delete("/api/v1/organizations/{organization_id}/connections/{provider}/{provider_target}")
def disconnect_provider_connection(
    organization_id: str,
    provider: str,
    provider_target: str,
    user: CurrentUser,
) -> dict[str, str]:
    require_organization_role(organization_id, user, frozenset({"controller", "operator"}))
    credential_ref = require_store().disconnect_connection(
        organization_id=organization_id,
        provider=provider,
        provider_target=provider_target,
    )
    if credential_ref:
        try:
            secret_store_from_environment().delete(credential_ref)
        except SecretStoreError:
            # The database state already prevents further provider use. Retain
            # no false-success claim about Vault cleanup, but do not revive the
            # connection because cleanup can be retried safely by operators.
            logger.warning("Disconnected %s connection for %s; Vault credential cleanup is pending", provider, organization_id)
            return {"status": "disconnected", "credential_cleanup": "pending"}
    return {"status": "disconnected", "credential_cleanup": "complete"}


@app.post("/api/v1/webhooks/plaid", status_code=202)
async def receive_plaid_webhook(
    request: Request,
    plaid_verification: Annotated[str | None, Header(alias="Plaid-Verification")] = None,
) -> dict[str, bool]:
    """Verify and durably deduplicate the configured Plaid webhook receiver."""
    signature = (plaid_verification or "").strip()
    if not signature:
        raise HTTPException(status_code=400, detail="Plaid-Verification is required")
    payload = await read_limited_request_body(request, maximum_bytes=_MAX_PLAID_WEBHOOK_BYTES)
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Plaid webhook payload is invalid") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=400, detail="Plaid webhook payload is invalid")
    event_id = decoded.get("request_id")
    if not isinstance(event_id, str) or not event_id:
        raise HTTPException(status_code=400, detail="Plaid webhook request_id is required")
    try:
        await asyncio.to_thread(
            require_plaid_webhook_verifier().verify,
            signature,
            payload,
        )
        accepted = await asyncio.to_thread(
            require_store().record_webhook_receipt,
            provider="plaid",
            provider_event_id=event_id,
            signature_verified=True,
            payload_hash=sha256(payload).hexdigest(),
            payload=decoded,
        )
    except (PlaidLinkError, PlaidWebhookError, SecretStoreError, PolicyError) as exc:
        logger.warning("Rejected Plaid webhook: %s", exc)
        raise HTTPException(status_code=401, detail="Plaid webhook verification failed") from exc
    return {"accepted": accepted}


@app.get("/api/v1/organizations/{organization_id}/close-mapping")
def get_close_mapping(organization_id: str, user: CurrentUser) -> dict[str, object] | None:
    require_organization_role(organization_id, user)
    return serialize_close_mapping(require_store().active_close_mapping(organization_id))


@app.post("/api/v1/organizations/{organization_id}/close-mapping", status_code=201)
def save_close_mapping(organization_id: str, request: CloseMappingRequest, user: CurrentUser) -> dict[str, object]:
    require_organization_role(organization_id, user, frozenset({"controller"}))
    mapping = require_store().save_close_mapping(
        organization_id=organization_id,
        mapping=request.to_domain(),
        approved_by_subject=user.subject,
    )
    return serialize_close_mapping(mapping) or {}


@app.get("/api/v1/organizations/{organization_id}/connections/plaid/link-token")
def create_plaid_link_token(organization_id: str, user: CurrentUser) -> dict[str, str | None]:
    require_organization_role(organization_id, user, frozenset({"controller", "operator"}))
    if plaid_link_client is None:
        raise HTTPException(status_code=503, detail="Plaid Link is not configured")
    try:
        token, expires_at = plaid_link_client.create_link_token(organization_id)
    except PlaidLinkError as exc:
        logger.warning("Plaid Link token creation failed for organization %s: %s", organization_id, exc)
        raise HTTPException(status_code=502, detail="Plaid Link could not be started") from exc
    return {"link_token": token, "expires_at": expires_at}


@app.post("/api/v1/organizations/{organization_id}/connections/plaid/exchange", status_code=201)
def exchange_plaid_public_token(
    organization_id: str,
    request: PlaidExchangeRequest,
    user: CurrentUser,
) -> list[dict[str, object]]:
    require_organization_role(organization_id, user, frozenset({"controller", "operator"}))
    if plaid_link_client is None:
        raise HTTPException(status_code=503, detail="Plaid Link is not configured")
    try:
        linked = plaid_link_client.exchange_public_token(request.public_token, request.selected_account_ids)
        access_ref = _connection_secret_ref("plaid", organization_id, linked.item_id, "access-token")
        plaid_link_client.secrets.store(access_ref, linked.access_token)
        now = datetime.now(timezone.utc)
        registered = []
        for account in linked.accounts:
            target = account.account_id
            health = connections.register(
                ConnectionHealth(
                    connection_id=f"plaid:{linked.item_id}:{target}",
                    organization_id=organization_id,
                    provider="plaid",
                    provider_environment="production",
                    provider_tenant_or_account_id=target,
                    status=ConnectionStatus.HEALTHY,
                    granted_scopes=("transactions",),
                    last_verified_at=now,
                    last_success_at=now,
                    remediation=None,
                ),
                credential_secret_ref=access_ref,
            )
            registered.append(
                require_store().upsert_connection(
                    connection_health=health,
                    credential_secret_ref=access_ref,
                    metadata={"plaid_item_id": linked.item_id},
                )
            )
    except (PlaidLinkError, SecretStoreError, PolicyError) as exc:
        logger.warning("Plaid Link exchange failed for organization %s: %s", organization_id, exc)
        raise HTTPException(status_code=502, detail="Plaid account connection could not be completed") from exc
    return [serialize_connection(connection) for connection in registered]


@app.get("/api/v1/organizations/{organization_id}/connections/google/authorize")
def authorize_google(organization_id: str, user: CurrentUser) -> dict[str, str]:
    require_organization_role(organization_id, user, frozenset({"controller", "operator"}))
    if service.deployment.mode == "production" and workflow_store is None:
        raise HTTPException(status_code=503, detail="durable workflow storage is required for production OAuth")
    if google_oauth_client is None:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    transaction = create_oauth_transaction("drive", google_oauth_client.config.redirect_uri)
    xero_oauth_sessions.put(transaction, organization_id)
    try:
        authorization_url = google_oauth_client.authorization_url(transaction)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=503, detail="Google OAuth configuration is invalid") from exc
    return {"authorization_url": authorization_url, "state": transaction.state}


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
        return _oauth_callback_error("xero", 503)
    if not state:
        return _oauth_callback_error("xero", 400)
    session = xero_oauth_sessions.consume(state)
    if session is None:
        return _oauth_callback_error("xero", 400)
    transaction, organization_id = session
    if error:
        return _oauth_callback_error("xero", 400, organization_id)
    if not code:
        return _oauth_callback_error("xero", 400, organization_id)
    try:
        validate_oauth_callback(transaction, state, xero_oauth_client.config.redirect_uri)
        # The state is a single-use opaque nonce.  Binding the Vault reference
        # to it prevents a later authorization from replacing credentials that
        # existing tenant connections still use.
        refresh_ref = _connection_secret_ref("xero", organization_id, state, "refresh-token")
        token = xero_oauth_client.exchange_code(
            code,
            transaction.code_verifier,
            refresh_token_secret_ref=refresh_ref,
        )
    except XeroOAuthError as exc:
        logger.warning("Xero OAuth token exchange failed for organization %s: %s", organization_id, exc)
        return _oauth_callback_error("xero", 502, organization_id)
    except PolicyError as exc:
        logger.warning("Xero OAuth callback validation failed for organization %s: %s", organization_id, exc)
        return _oauth_callback_error("xero", 400, organization_id)
    if token.scope is not None and frozenset(token.scope.split()) != frozenset(xero_oauth_client.config.scopes):
        _discard_xero_refresh_token(refresh_ref)
        return _oauth_callback_error("xero", 502, organization_id)
    try:
        registered_tenants = _register_xero_connection(organization_id, refresh_ref)
    except (XeroOAuthError, PolicyError) as exc:
        logger.warning("Xero tenant registration failed after OAuth: %s", exc)
        _discard_xero_refresh_token(refresh_ref)
        return _oauth_callback_error("xero", 502, organization_id)
    if registered_tenants == 0:
        _discard_xero_refresh_token(refresh_ref)
        return _oauth_callback_error("xero", 502, organization_id)
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


@app.get("/api/v1/connections/google/callback")
def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    if google_oauth_client is None:
        return _oauth_callback_error("google", 503)
    if not state:
        return _oauth_callback_error("google", 400)
    session = xero_oauth_sessions.consume(state)
    if session is None:
        return _oauth_callback_error("google", 400)
    transaction, organization_id = session
    if transaction.provider != "drive":
        return _oauth_callback_error("google", 400, organization_id)
    if error:
        return _oauth_callback_error("google", 400, organization_id)
    if not code:
        return _oauth_callback_error("google", 400, organization_id)
    try:
        validate_oauth_callback(transaction, state, google_oauth_client.config.redirect_uri)
        token = google_oauth_client.exchange_code(code, transaction)
        if token.scope:
            granted = frozenset(token.scope.split())
            if not frozenset(google_oauth_client.config.scopes).issubset(granted):
                raise GoogleOAuthError("Google granted scopes do not match the approved scope profile")
        if not token.refresh_token:
            raise GoogleOAuthError("Google did not grant a durable refresh token")
        refresh_ref = _connection_secret_ref("google", organization_id, "workspace", "refresh-token")
        access_ref = _connection_secret_ref("google", organization_id, "workspace", "access-token")
        google_oauth_client.secrets.store(refresh_ref, token.refresh_token)
        google_oauth_client.secrets.store(access_ref, token.access_token)
        now = datetime.now(timezone.utc)
        scope = google_oauth_client.config.scopes
        for provider in ("drive", "gmail"):
            health = connections.register(
                ConnectionHealth(
                    connection_id=f"google:{organization_id}:{provider}",
                    organization_id=organization_id,
                    provider=provider,
                    provider_environment="production",
                    provider_tenant_or_account_id="workspace",
                    status=ConnectionStatus.HEALTHY,
                    granted_scopes=scope,
                    last_verified_at=now,
                    last_success_at=now,
                    remediation=None,
                ),
                credential_secret_ref=refresh_ref,
            )
            require_store().upsert_connection(connection_health=health, credential_secret_ref=refresh_ref)
    except (GoogleOAuthError, SecretStoreError, PolicyError) as exc:
        logger.warning("Google OAuth callback failed for organization %s: %s", organization_id, exc)
        return _oauth_callback_error("google", 502, organization_id)
    payload = {"status": "authorized", "organization_id": organization_id}
    web_app_url = _web_app_url()
    if web_app_url:
        return RedirectResponse(
            f"{web_app_url}/?{urlencode({'google': payload['status'], 'organization_id': organization_id})}",
            status_code=303,
        )
    return payload


def _xero_tenant_allowlist() -> frozenset[str]:
    raw = os.getenv("ACCOUNTINGOS_XERO_TENANT_ALLOWLIST", "")
    return frozenset(
        token for token in raw.replace(",", " ").split() if token and not token.startswith("replace-")
    )


def _register_xero_connection(organization_id: str, refresh_token_secret_ref: str | None = None) -> int:
    if xero_oauth_client is None:
        raise XeroOAuthError("Xero OAuth is not configured")
    if not refresh_token_secret_ref or not refresh_token_secret_ref.startswith("secret://"):
        raise XeroOAuthError("Xero connection registration requires a tenant refresh-token reference")
    allowlist = _xero_tenant_allowlist()
    provider_environment = "demo" if service.deployment.mode == "demo" else "production"
    if provider_environment == "production" and not allowlist:
        raise XeroOAuthError(
            "ACCOUNTINGOS_XERO_TENANT_ALLOWLIST must name an approved tenant before production authorization"
        )
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
                credential_secret_ref=refresh_token_secret_ref,
            )
            if workflow_store is not None:
                workflow_store.upsert_connection(
                    connection_health=connection_health,
                    credential_secret_ref=refresh_token_secret_ref,
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
    review = require_store().review_data_for_run(run_id)
    if review.mapping is None:
        raise HTTPException(status_code=409, detail="the close run has no frozen approved mapping")
    permitted_codes = frozenset(
        str(item) for item in review.mapping.configuration.get("permitted_journal_account_codes", [])
    )
    if not permitted_codes:
        raise HTTPException(status_code=409, detail="the close mapping has no permitted journal account codes")
    domain_proposals = tuple(
        build_journal_proposal(
            item.proposal_id,
            item.journal_date,
            item.narration,
            (
                JournalLine(line.account_code, line.debit, line.credit, tuple(line.evidence_ids))
                for line in item.lines
            ),
            permitted_codes,
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
    require_configured_controller(run.organization_id, user)
    if request.decision == "changes_requested":
        if not request.comment or len(request.comment.strip()) < 3:
            raise HTTPException(status_code=422, detail="a change request needs a comment")
        return serialize_persisted_run(
            require_store().request_review_changes(
                run_id=run_id,
                package_hash=request.package_hash,
                actor_subject=user.subject,
                comment=request.comment.strip(),
            )
        )
    return serialize_persisted_run(
        require_store().approve_review_package(
            run_id=run_id,
            package_hash=request.package_hash,
            actor_subject=user.subject,
            comment=request.comment.strip() if request.comment else "",
        )
    )
