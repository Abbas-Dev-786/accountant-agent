"""Process durable AccountingOS task rows without exposing provider access to HTTP.

The worker owns a short database lease before it invokes a task handler. A
handler can be wired to Xero/Plaid/Google/B2/Groq at deployment time, but the
queue mechanics are intentionally provider-agnostic and fail closed when a
handler is absent or configuration is incomplete.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Protocol

from .domain import CloseRun, CloseService, DeploymentConfig, RunState
from .evidence import EvidenceCollector, EvidenceScope
from .provider_runtime import GmailHttpClient, GoogleDriveHttpClient
from .provider_runtime import (
    PlaidHttpSandboxClient,
    PlaidProductionHttpClient,
    UrllibJsonTransport,
    XeroDemoHttpClient,
    XeroProductionHttpClient,
)
from .providers import PlaidProductionAdapter, PlaidSandboxAdapter, ProviderReadError, XeroDemoAdapter, XeroProductionAdapter
from .secrets_store import SecretStoreError, secret_store_from_environment
from .supabase_db import PersistedTask
from .xero_oauth import XeroOAuthClient, XeroOAuthConfig, XeroOAuthError


class TaskBlocked(RuntimeError):
    """A visible, operator-actionable condition prevented task execution."""


class DurableTaskStore(Protocol):
    def claim_next_task(self, worker_id: str, *, lease_seconds: int = 60) -> PersistedTask | None:
        ...

    def complete_task(self, task: PersistedTask, worker_id: str) -> None:
        ...

    def block_task(self, task: PersistedTask, worker_id: str, error: str) -> None:
        ...


class TaskExecutor(Protocol):
    def execute(self, task: PersistedTask) -> None:
        ...


@dataclass(frozen=True)
class WorkerResult:
    task_id: str | None
    task_key: str | None
    status: str
    error: str | None = None


class DurableWorkflowWorker:
    """Claim one task, execute it once, and persist a terminal result.

    Unknown exceptions are intentionally reduced to a stable, non-sensitive
    blocker. Provider payloads and credentials must never enter task events or
    controller-visible errors.
    """

    def __init__(
        self,
        store: DurableTaskStore,
        executor: TaskExecutor,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("worker id and lease duration are required")
        self.store = store
        self.executor = executor
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def process_once(self) -> WorkerResult:
        task = self.store.claim_next_task(self.worker_id, lease_seconds=self.lease_seconds)
        if task is None:
            return WorkerResult(None, None, "idle")
        try:
            self.executor.execute(task)
        except TaskBlocked as exc:
            message = str(exc) or "task is blocked pending operator action"
            self.store.block_task(task, self.worker_id, message)
            return WorkerResult(task.task_id, task.task_key, "blocked", message)
        except Exception:
            message = "task execution failed; inspect the server-side provider and worker logs"
            self.store.block_task(task, self.worker_id, message)
            return WorkerResult(task.task_id, task.task_key, "blocked", message)
        self.store.complete_task(task, self.worker_id)
        return WorkerResult(task.task_id, task.task_key, "succeeded")


class RegisteredTaskExecutor:
    """Explicit task registry used by the deployment-specific worker entrypoint."""

    def __init__(self, handlers: dict[str, TaskExecutor]) -> None:
        self.handlers = dict(handlers)

    def execute(self, task: PersistedTask) -> None:
        handler = self.handlers.get(task.task_key)
        if handler is None:
            raise TaskBlocked(f"no worker handler is configured for {task.task_key}")
        handler.execute(task)


class EnvironmentPreflightExecutor:
    """Check provider secrets before any fixture source read is attempted."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self.env = os.environ if env is None else env

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "preflight":
            raise TaskBlocked(f"preflight executor cannot run {task.task_key}")
        try:
            xero = XeroOAuthConfig.from_environment(self.env)
            secrets = secret_store_from_environment(self.env)
            secrets.resolve(xero.client_secret_ref)
            secrets.resolve(xero.refresh_token_secret_ref)
            required = ("PLAID_CLIENT_ID", "PLAID_SECRET_REF", "PLAID_ACCESS_TOKEN_REF", "PLAID_ITEM_ID")
            missing = [name for name in required if not self.env.get(name, "").strip() or "replace-with" in self.env.get(name, "")]
            if missing:
                raise TaskBlocked(f"missing required fixture configuration: {', '.join(missing)}")
            secrets.resolve(self.env["PLAID_SECRET_REF"])
            secrets.resolve(self.env["PLAID_ACCESS_TOKEN_REF"])
        except (XeroOAuthError, SecretStoreError) as exc:
            raise TaskBlocked(str(exc)) from exc


class ProductionPreflightExecutor(EnvironmentPreflightExecutor):
    """Fail closed until every US production source is explicitly configured."""

    def execute(self, task: PersistedTask) -> None:
        super().execute(task)
        selected_accounts = _configured_values(self.env, "ACCOUNTINGOS_PLAID_SELECTED_ACCOUNT_IDS")
        tenant_id = self.env.get("ACCOUNTINGOS_XERO_TENANT_ID", "").strip()
        if not tenant_id or tenant_id.startswith("replace-with"):
            raise TaskBlocked("ACCOUNTINGOS_XERO_TENANT_ID must identify the approved production tenant")
        if not selected_accounts:
            raise TaskBlocked("ACCOUNTINGOS_PLAID_SELECTED_ACCOUNT_IDS must contain the approved production accounts")
        if any(value.startswith("replace-with") for value in selected_accounts):
            raise TaskBlocked("Plaid selected-account configuration contains a placeholder")
        try:
            secrets = secret_store_from_environment(self.env)
            google_ref = self.env.get("GOOGLE_ACCESS_TOKEN_REF", "").strip()
            if not google_ref:
                raise TaskBlocked("GOOGLE_ACCESS_TOKEN_REF is required for scoped evidence collection")
            secrets.resolve(google_ref)
        except SecretStoreError as exc:
            raise TaskBlocked("a required production secret reference is unavailable") from exc


def _configured_values(env: Mapping[str, str], key: str) -> frozenset[str]:
    return frozenset(value for value in env.get(key, "").replace(",", " ").split() if value)


class SourceSnapshotStore(Protocol):
    def get_close_run(self, run_id: str):
        ...

    def connections_for_organization(self, organization_id: str):
        ...

    def persist_source_snapshot(self, **kwargs):
        ...


class DemoSourceSyncExecutor:
    """Read Xero Demo Company and Plaid Sandbox, then commit one snapshot."""

    def __init__(
        self,
        store: SourceSnapshotStore,
        deployment: DeploymentConfig,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.deployment = deployment
        self.env = os.environ if env is None else env

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "synchronize_sources":
            raise TaskBlocked(f"source sync executor cannot run {task.task_key}")
        run = self.store.get_close_run(task.run_id)
        if run is None or run.state != "synchronizing":
            raise TaskBlocked("close run is not available for source synchronization")
        try:
            xero_config = XeroOAuthConfig.from_environment(self.env)
            secrets = secret_store_from_environment(self.env)
            connections = self.store.connections_for_organization(run.organization_id)
            xero_connections = [
                item
                for item in connections
                if item.provider == "xero" and item.provider_environment == "demo" and item.status == "healthy"
            ]
            if len(xero_connections) != 1:
                raise TaskBlocked("exactly one healthy Xero Demo Company connection is required")
            tenant_id = xero_connections[0].provider_tenant_or_account_id
            configured_tenant = self.env.get("ACCOUNTINGOS_XERO_DEMO_TENANT_ID", "").strip()
            if tenant_id != configured_tenant:
                raise TaskBlocked("connected Xero tenant does not match ACCOUNTINGOS_XERO_DEMO_TENANT_ID")
            xero_oauth = XeroOAuthClient(xero_config, secrets)
            xero = XeroDemoAdapter(
                XeroDemoHttpClient(
                    tenant_id=tenant_id,
                    access_token_secret_ref=xero_config.refresh_token_secret_ref,
                    secret_resolver=secrets,
                    transport=UrllibJsonTransport(),
                    oauth_client=xero_oauth,
                ),
                tenant_id,
            )
            plaid_access_ref = self.env.get("PLAID_ACCESS_TOKEN_REF", "").strip()
            plaid = PlaidSandboxAdapter(
                PlaidHttpSandboxClient(
                    client_id=self.env.get("PLAID_CLIENT_ID", "").strip(),
                    client_secret_secret_ref=self.env.get("PLAID_SECRET_REF", "").strip(),
                    secret_resolver=secrets,
                    transport=UrllibJsonTransport(),
                ),
                secrets.resolve(plaid_access_ref),
            )
            domain_run = CloseRun(
                task.run_id,
                run.organization_id,
                run.period_start,
                run.period_end,
                self.deployment,
                state=RunState.SYNCHRONIZING,
            )
            close_service = CloseService(self.deployment)
            xero_batch = xero.read_batch()
            plaid_batch = plaid.read_batch()
            snapshot = close_service.build_snapshot(domain_run, (xero_batch, plaid_batch))
            self.store.persist_source_snapshot(
                run_id=task.run_id,
                batches=(xero_batch, plaid_batch),
                snapshot=snapshot,
                provider_identities={"xero": tenant_id, "plaid": self.env.get("PLAID_ITEM_ID", "").strip()},
            )
        except TaskBlocked:
            raise
        except (XeroOAuthError, SecretStoreError, ValueError) as exc:
            raise TaskBlocked("source provider configuration is invalid") from exc


class ProductionSourceSyncExecutor:
    """Read the approved US Xero tenant and Plaid Production Item atomically."""

    def __init__(
        self,
        store: SourceSnapshotStore,
        deployment: DeploymentConfig,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if (deployment.mode, deployment.data_class, deployment.market, deployment.currency) != (
            "production",
            "live",
            "US",
            "USD",
        ):
            raise ValueError("production source sync requires a live US/USD deployment")
        self.store = store
        self.deployment = deployment
        self.env = os.environ if env is None else env

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "synchronize_sources":
            raise TaskBlocked(f"source sync executor cannot run {task.task_key}")
        run = self.store.get_close_run(task.run_id)
        if run is None or run.state != "synchronizing":
            raise TaskBlocked("close run is not available for source synchronization")
        if (run.deployment_mode, run.data_class) != ("production", "live"):
            raise TaskBlocked("production worker refuses a non-live close run")
        try:
            xero_config = XeroOAuthConfig.from_environment(self.env)
            secrets = secret_store_from_environment(self.env)
            expected_tenant = self.env.get("ACCOUNTINGOS_XERO_TENANT_ID", "").strip()
            if not expected_tenant or expected_tenant.startswith("replace-with"):
                raise TaskBlocked("approved Xero tenant is not configured")
            connections = self.store.connections_for_organization(run.organization_id)
            xero_connections = [
                item
                for item in connections
                if item.provider == "xero"
                and item.provider_environment == "production"
                and item.status == "healthy"
                and item.provider_tenant_or_account_id == expected_tenant
            ]
            if len(xero_connections) != 1:
                raise TaskBlocked("exactly one healthy approved Xero production connection is required")
            selected_accounts = _configured_values(self.env, "ACCOUNTINGOS_PLAID_SELECTED_ACCOUNT_IDS")
            if not selected_accounts:
                raise TaskBlocked("approved Plaid account selection is not configured")
            xero_oauth = XeroOAuthClient(xero_config, secrets)
            xero = XeroProductionAdapter(
                XeroProductionHttpClient(
                    tenant_id=expected_tenant,
                    access_token_secret_ref=xero_config.refresh_token_secret_ref,
                    secret_resolver=secrets,
                    transport=UrllibJsonTransport(),
                    oauth_client=xero_oauth,
                ),
                expected_tenant,
            )
            plaid_access_ref = self.env.get("PLAID_ACCESS_TOKEN_REF", "").strip()
            plaid = PlaidProductionAdapter(
                PlaidProductionHttpClient(
                    client_id=self.env.get("PLAID_CLIENT_ID", "").strip(),
                    client_secret_secret_ref=self.env.get("PLAID_SECRET_REF", "").strip(),
                    secret_resolver=secrets,
                    transport=UrllibJsonTransport(),
                ),
                secrets.resolve(plaid_access_ref),
            )
            domain_run = CloseRun(
                task.run_id,
                run.organization_id,
                run.period_start,
                run.period_end,
                self.deployment,
                state=RunState.SYNCHRONIZING,
            )
            close_service = CloseService(self.deployment)
            xero_batch = xero.read_batch()
            plaid_batch = plaid.read_batch()
            _validate_selected_plaid_accounts(plaid_batch, selected_accounts)
            snapshot = close_service.build_snapshot(domain_run, (xero_batch, plaid_batch))
            self.store.persist_source_snapshot(
                run_id=task.run_id,
                batches=(xero_batch, plaid_batch),
                snapshot=snapshot,
                provider_identities={
                    "xero": expected_tenant,
                    "plaid": self.env.get("PLAID_ITEM_ID", "").strip(),
                },
            )
        except TaskBlocked:
            raise
        except (ProviderReadError, XeroOAuthError, SecretStoreError, ValueError) as exc:
            raise TaskBlocked("production source synchronization could not complete; inspect server-side provider logs") from exc


def _validate_selected_plaid_accounts(batch, selected_accounts: frozenset[str]) -> None:
    """Reject a source batch containing an unapproved account before persistence."""
    import json

    for record in batch.record_versions:
        payload = json.loads(record.payload_json)
        if payload.get("removed") is True:
            continue
        account_id = payload.get("account_id")
        if not isinstance(account_id, str) or account_id not in selected_accounts:
            raise TaskBlocked("Plaid source contains a transaction outside the approved account selection")


class EvidenceStore(Protocol):
    def get_close_run(self, run_id: str):
        ...

    def persist_evidence_batch(self, **kwargs) -> None:
        ...


class GoogleEvidenceExecutor:
    """Collect only configured Workspace evidence metadata for the close period."""

    def __init__(self, store: EvidenceStore, env: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.env = os.environ if env is None else env

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "collect_evidence":
            raise TaskBlocked(f"evidence executor cannot run {task.task_key}")
        run = self.store.get_close_run(task.run_id)
        if run is None or run.snapshot_id is None:
            raise TaskBlocked("evidence collection requires the committed source snapshot")
        try:
            access_token_ref = self.env.get("GOOGLE_ACCESS_TOKEN_REF", "").strip()
            folders = frozenset(
                item for item in self.env.get("ACCOUNTINGOS_GOOGLE_DRIVE_FOLDER_IDS", "").replace(",", " ").split() if item
            )
            mailbox = self.env.get("ACCOUNTINGOS_GOOGLE_GMAIL_MAILBOX", "").strip()
            labels = frozenset(
                item for item in self.env.get("ACCOUNTINGOS_GOOGLE_GMAIL_LABELS", "").replace(",", " ").split() if item
            )
            if not access_token_ref or not folders or not mailbox or not labels:
                raise TaskBlocked("Google evidence scope configuration is incomplete")
            secrets = secret_store_from_environment(self.env)
            secrets.resolve(access_token_ref)
            scope = EvidenceScope(folders, mailbox, labels, date.fromisoformat(run.period_start), date.fromisoformat(run.period_end))
            transport = UrllibJsonTransport()
            batch = EvidenceCollector(
                GoogleDriveHttpClient(access_token_ref, secrets, transport),
                GmailHttpClient(access_token_ref, secrets, transport),
            ).collect(scope)
            self.store.persist_evidence_batch(run_id=task.run_id, batch=batch)
        except TaskBlocked:
            raise
        except (SecretStoreError, ValueError) as exc:
            raise TaskBlocked("Google evidence configuration is invalid") from exc


class ReconciliationMappingGateExecutor:
    """Make the required accountant-controlled mapping a visible workflow gate.

    The current source contract deliberately does not infer a ledger mapping
    from provider payloads.  Doing so would turn an implementation guess into
    an accounting control.  Until a versioned mapping is persisted and wired to
    the reconciliation engine, this task must expose that fact rather than
    being reported as an unconfigured worker handler.
    """

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "reconcile":
            raise TaskBlocked(f"reconciliation gate cannot run {task.task_key}")
        raise TaskBlocked(
            "reconciliation requires a versioned accountant-approved bank-to-ledger mapping, "
            "matching tolerances, and permitted account codes"
        )
