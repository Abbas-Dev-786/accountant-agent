"""Process durable AccountingOS task rows without exposing provider access to HTTP.

The worker owns a short database lease before it invokes a task handler. A
handler can be wired to Xero/Plaid/Google/B2/Groq at deployment time, but the
queue mechanics are intentionally provider-agnostic and fail closed when a
handler is absent or configuration is incomplete.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from threading import Event, Thread
from typing import Mapping, Protocol

from .actions import XeroDraftRequest
from .ai import ExplanationContext, GroundedExplanationService, GroundedFact
from .b2 import B2Config, B2Error, B2ObjectLockClient
from .close_mapping import PersistedCloseMapping
from .connections import ConnectionHealth, ConnectionStatus
from .close_execution import CloseExecutionError, derive_close_execution
from .domain import CloseRun, CloseService, DeploymentConfig, RunState
from .evidence import (
    ChecklistRequirement,
    ChecklistVersion,
    EmailPolicy,
    EmailRequest,
    EmailTemplate,
    EvidenceCollector,
    EvidencePolicyError,
    EvidenceScope,
    evaluate_checklist,
)
from .google_oauth import GoogleOAuthClient, GoogleOAuthConfig, GoogleOAuthError
from .groq import GroqConfig, GroqError, GroqExplanationModel
from .plaid_link import PlaidLinkConfig, PlaidLinkError
from .provider_runtime import GmailHttpClient, GoogleDriveHttpClient, XeroDraftHttpClient
from .provider_runtime import (
    PlaidHttpSandboxClient,
    PlaidProductionHttpClient,
    UrllibJsonTransport,
    XeroDemoHttpClient,
    XeroProductionHttpClient,
)
from .providers import (
    PlaidCursorState,
    PlaidProductionAdapter,
    PlaidSandboxAdapter,
    ProviderReadError,
    XeroDemoAdapter,
    XeroProductionAdapter,
)
from .secrets_store import SecretStoreError, secret_store_from_environment
from .supabase_db import PersistedPlaidSyncRequest, PersistedTask
from .xero_oauth import XeroOAuthClient, XeroOAuthConfig, XeroOAuthError


class TaskBlocked(RuntimeError):
    """A visible, operator-actionable condition prevented task execution."""


class DurableTaskStore(Protocol):
    def claim_next_task(self, worker_id: str, *, lease_seconds: int = 60) -> PersistedTask | None:
        ...

    def complete_task(self, task: PersistedTask, worker_id: str) -> None:
        ...

    def renew_task_lease(self, task: PersistedTask, worker_id: str, *, lease_seconds: int = 60) -> bool:
        ...

    def block_task(self, task: PersistedTask, worker_id: str, error: str) -> None:
        ...

    def retry_task(self, task: PersistedTask, worker_id: str, error: str) -> str:
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

    Unexpected exceptions are reduced to stable, non-sensitive retry messages.
    Explicit TaskBlocked failures remain controller-actionable blockers; neither
    path exposes provider payloads or credentials in task events.
    """

    def __init__(
        self,
        store: DurableTaskStore,
        executor: TaskExecutor,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("worker id and lease duration are required")
        if heartbeat_interval_seconds is not None and heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.store = store
        self.executor = executor
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or max(0.25, min(30.0, lease_seconds / 3))

    def process_once(self) -> WorkerResult:
        task = self.store.claim_next_task(self.worker_id, lease_seconds=self.lease_seconds)
        if task is None:
            return WorkerResult(None, None, "idle")
        heartbeat = _LeaseHeartbeat(
            self.store,
            task,
            self.worker_id,
            self.lease_seconds,
            self.heartbeat_interval_seconds,
        )
        heartbeat.start()
        blocked_error: str | None = None
        retry_error: str | None = None
        try:
            self.executor.execute(task)
        except TaskBlocked as exc:
            blocked_error = str(exc) or "task is blocked pending operator action"
        except Exception:
            retry_error = "task execution failed; retrying within the persisted attempt budget"
        finally:
            heartbeat.stop()
        if heartbeat.lease_lost:
            return WorkerResult(
                task.task_id,
                task.task_key,
                "lease_lost",
                "task lease could not be renewed; the task was left for safe recovery",
            )
        if blocked_error:
            self.store.block_task(task, self.worker_id, blocked_error)
            return WorkerResult(task.task_id, task.task_key, "blocked", blocked_error)
        if retry_error:
            state = self.store.retry_task(task, self.worker_id, retry_error)
            status = "retrying" if state == "ready" else "failed"
            return WorkerResult(task.task_id, task.task_key, status, retry_error)
        self.store.complete_task(task, self.worker_id)
        return WorkerResult(task.task_id, task.task_key, "succeeded")


class _LeaseHeartbeat:
    """Renew a durable task lease while a synchronous executor is running."""

    def __init__(
        self,
        store: DurableTaskStore,
        task: PersistedTask,
        worker_id: str,
        lease_seconds: int,
        interval_seconds: float,
    ) -> None:
        self.store = store
        self.task = task
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread = Thread(target=self._run, name=f"accountingos-lease-{task.task_id}", daemon=True)
        self.lease_lost = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                if not self.store.renew_task_lease(
                    self.task,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                ):
                    self.lease_lost = True
                    return
            except Exception:
                # A database failure means ownership can no longer be proven.
                # Do not let this worker commit a terminal state after the
                # executor returns; recovery will reclaim the expired task.
                self.lease_lost = True
                return


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
            secrets.resolve(xero.refresh_token_secret_ref)
            PlaidLinkConfig.from_environment(self.env)
            required = ("PLAID_ACCESS_TOKEN_REF", "PLAID_ITEM_ID")
            missing = [name for name in required if not self.env.get(name, "").strip() or "replace-with" in self.env.get(name, "")]
            if missing:
                raise TaskBlocked(f"missing required fixture configuration: {', '.join(missing)}")
            secrets.resolve(self.env["PLAID_ACCESS_TOKEN_REF"])
        except (XeroOAuthError, SecretStoreError) as exc:
            raise TaskBlocked(str(exc)) from exc


class ProductionPreflightExecutor:
    """Fail closed until every US production source is explicitly configured."""

    def __init__(self, store, env: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.env = os.environ if env is None else env

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "preflight":
            raise TaskBlocked(f"preflight executor cannot run {task.task_key}")
        run = self.store.get_close_run(task.run_id)
        if run is None:
            raise TaskBlocked("close run is unavailable for production preflight")
        mapping = self.store.mapping_for_run(task.run_id)
        if mapping is None:
            raise TaskBlocked("an accountant-approved close mapping is required before production preflight")
        configuration = _mapping_configuration(mapping)
        tenant_id = str(configuration["xero_tenant_id"])
        selected_accounts = _mapping_account_ids(configuration)
        try:
            secrets = secret_store_from_environment(self.env)
            xero_config = XeroOAuthConfig.from_environment(self.env)
            xero_refresh_ref = self.store.connection_secret_ref(run.organization_id, "xero", tenant_id)
            if not xero_refresh_ref:
                raise TaskBlocked("the mapped Xero tenant is not a healthy production connection")
            secrets.resolve(xero_refresh_ref)
            PlaidLinkConfig.from_environment(self.env)
            access_refs = {
                self.store.connection_secret_ref(run.organization_id, "plaid", account_id)
                for account_id in selected_accounts
            }
            if None in access_refs or len(access_refs) != 1:
                raise TaskBlocked("selected Plaid accounts must belong to one healthy production Item")
            secrets.resolve(next(iter(access_refs)))
            google_refresh_ref = self.store.connection_secret_ref(run.organization_id, "drive", "workspace")
            if not google_refresh_ref:
                raise TaskBlocked("Google Workspace is not a healthy production connection")
            GoogleOAuthConfig.from_environment(self.env)
            secrets.resolve(google_refresh_ref)
        except (GoogleOAuthError, PlaidLinkError, SecretStoreError, XeroOAuthError) as exc:
            raise TaskBlocked("required production provider credentials are unavailable") from exc


def _configured_values(env: Mapping[str, str], key: str) -> frozenset[str]:
    return frozenset(value for value in env.get(key, "").replace(",", " ").split() if value)


class SourceSnapshotStore(Protocol):
    def get_close_run(self, run_id: str):
        ...

    def connections_for_organization(self, organization_id: str):
        ...

    def mapping_for_run(self, run_id: str):
        ...

    def connection_secret_ref(self, organization_id: str, provider: str, provider_target: str) -> str | None:
        ...

    def plaid_item_id_for_accounts(self, organization_id: str, account_ids):
        ...

    def plaid_sync_state_for_item(self, organization_id: str, item_id: str):
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
        if run.snapshot_id:
            return
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
                    client_secret=PlaidLinkConfig.from_environment(self.env).client_secret,
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
        if run.snapshot_id:
            return
        if (run.deployment_mode, run.data_class) != ("production", "live"):
            raise TaskBlocked("production worker refuses a non-live close run")
        try:
            xero_config = XeroOAuthConfig.from_environment(self.env)
            secrets = secret_store_from_environment(self.env)
            mapping_reader = getattr(self.store, "mapping_for_run", None)
            mapping = mapping_reader(task.run_id) if callable(mapping_reader) else None
            if mapping is not None:
                configuration = _mapping_configuration(mapping)
                expected_tenant = str(configuration["xero_tenant_id"])
                selected_accounts = _mapping_account_ids(configuration)
            else:
                # Compatibility for isolated adapter tests. The deployed
                # Supabase store always exposes a persisted mapping.
                expected_tenant = self.env.get("ACCOUNTINGOS_XERO_TENANT_ID", "").strip()
                selected_accounts = _configured_values(self.env, "ACCOUNTINGOS_PLAID_SELECTED_ACCOUNT_IDS")
                if not expected_tenant or expected_tenant.startswith("replace-with") or not selected_accounts:
                    raise TaskBlocked("an accountant-approved close mapping is required before source synchronization")
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
            credential_lookup = getattr(self.store, "connection_secret_ref", None)
            xero_refresh_ref = credential_lookup(run.organization_id, "xero", expected_tenant) if callable(credential_lookup) else None
            if mapping is not None and not xero_refresh_ref:
                raise TaskBlocked("mapped Xero connection credentials are unavailable")
            if mapping is not None:
                xero_config = replace(xero_config, refresh_token_secret_ref=xero_refresh_ref)
            xero_oauth = XeroOAuthClient(xero_config, secrets)
            xero = XeroProductionAdapter(
                XeroProductionHttpClient(
                    tenant_id=expected_tenant,
                    access_token_secret_ref=xero_config.refresh_token_secret_ref,
                    secret_resolver=secrets,
                    transport=UrllibJsonTransport(),
                    oauth_client=xero_oauth,
                    period_start=date.fromisoformat(run.period_start),
                    period_end=date.fromisoformat(run.period_end),
                ),
                expected_tenant,
            )
            if mapping is not None:
                plaid_access_refs = {
                    credential_lookup(run.organization_id, "plaid", account_id)
                    for account_id in selected_accounts
                }
                if None in plaid_access_refs or len(plaid_access_refs) != 1:
                    raise TaskBlocked("selected Plaid accounts must belong to one healthy production Item")
                plaid_access_ref = next(iter(plaid_access_refs))
                item_lookup = getattr(self.store, "plaid_item_id_for_accounts", None)
                plaid_item_id = item_lookup(run.organization_id, tuple(selected_accounts)) if callable(item_lookup) else None
                if not plaid_item_id:
                    raise TaskBlocked("selected Plaid accounts are missing their production Item identity")
            else:
                plaid_access_ref = self.env.get("PLAID_ACCESS_TOKEN_REF", "").strip()
                plaid_item_id = self.env.get("PLAID_ITEM_ID", "").strip()
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
            sync_state_reader = getattr(self.store, "plaid_sync_state_for_item", None)
            persisted_state = (
                sync_state_reader(run.organization_id, plaid_item_id)
                if callable(sync_state_reader)
                else None
            )
            plaid = PlaidProductionAdapter(
                PlaidProductionHttpClient(
                    client_id=self.env.get("PLAID_CLIENT_ID", "").strip(),
                    client_secret=PlaidLinkConfig.from_environment(self.env).client_secret,
                    transport=UrllibJsonTransport(),
                ),
                secrets.resolve(plaid_access_ref),
                state=PlaidCursorState(
                    persisted_state.cursor if persisted_state is not None else None,
                    dict(persisted_state.records) if persisted_state is not None else {},
                ),
            )
            plaid_batch = plaid.read_batch()
            _validate_selected_plaid_accounts(plaid_batch, selected_accounts)
            snapshot = close_service.build_snapshot(domain_run, (xero_batch, plaid_batch))
            self.store.persist_source_snapshot(
                run_id=task.run_id,
                batches=(xero_batch, plaid_batch),
                snapshot=snapshot,
                provider_identities={
                    "xero": expected_tenant,
                    "plaid": plaid_item_id,
                },
                plaid_sync_state={
                    "item_id": plaid_item_id,
                    "cursor": plaid.state.cursor,
                    "records": plaid.state.records,
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
        account_id = payload.get("account_id")
        if not isinstance(account_id, str) or account_id not in selected_accounts:
            raise TaskBlocked("Plaid source contains a transaction outside the approved account selection")


class PlaidWebhookSyncStore(Protocol):
    def claim_next_plaid_sync_request(self, worker_id: str, *, lease_seconds: int = 60) -> PersistedPlaidSyncRequest | None:
        ...

    def complete_plaid_sync_request(self, request: PersistedPlaidSyncRequest, worker_id: str) -> None:
        ...

    def block_plaid_sync_request(self, request: PersistedPlaidSyncRequest, worker_id: str, error: str) -> None:
        ...

    def retry_plaid_sync_request(self, request: PersistedPlaidSyncRequest, worker_id: str, error: str) -> str:
        ...

    def plaid_accounts_for_item(self, organization_id: str, item_id: str):
        ...

    def connection_secret_ref(self, organization_id: str, provider: str, provider_target: str) -> str | None:
        ...

    def plaid_sync_state_for_item(self, organization_id: str, item_id: str):
        ...

    def persist_plaid_sync_state(self, **kwargs) -> None:
        ...


class PlaidWebhookSyncWorker:
    """Advance persisted Plaid Transactions Sync state for verified webhook events."""

    def __init__(
        self,
        store: PlaidWebhookSyncStore,
        *,
        worker_id: str,
        env: Mapping[str, str] | None = None,
        lease_seconds: int = 300,
    ) -> None:
        self.store = store
        self.worker_id = worker_id
        self.env = os.environ if env is None else env
        self.lease_seconds = lease_seconds

    def process_once(self) -> WorkerResult:
        request = self.store.claim_next_plaid_sync_request(self.worker_id, lease_seconds=self.lease_seconds)
        if request is None:
            return WorkerResult(None, None, "idle")
        try:
            self._sync(request)
        except TaskBlocked as exc:
            error = str(exc) or "Plaid webhook sync is blocked pending operator action"
            self.store.block_plaid_sync_request(request, self.worker_id, error)
            return WorkerResult(request.request_id, "sync_plaid_webhook", "blocked", error)
        except Exception:
            error = "Plaid webhook sync failed; retrying within the persisted attempt budget"
            state = self.store.retry_plaid_sync_request(request, self.worker_id, error)
            return WorkerResult(request.request_id, "sync_plaid_webhook", "retrying" if state == "ready" else "failed", error)
        self.store.complete_plaid_sync_request(request, self.worker_id)
        return WorkerResult(request.request_id, "sync_plaid_webhook", "succeeded")

    def _sync(self, request: PersistedPlaidSyncRequest) -> None:
        try:
            secrets = secret_store_from_environment(self.env)
            plaid_config = PlaidLinkConfig.from_environment(self.env)
            account_ids = frozenset(str(value) for value in self.store.plaid_accounts_for_item(request.organization_id, request.item_id))
            if not account_ids:
                raise TaskBlocked("Plaid webhook Item is no longer connected to a healthy production account")
            access_refs = {
                self.store.connection_secret_ref(request.organization_id, "plaid", account_id)
                for account_id in account_ids
            }
            if None in access_refs or len(access_refs) != 1:
                raise TaskBlocked("Plaid webhook Item does not have one healthy production access token")
            state = self.store.plaid_sync_state_for_item(request.organization_id, request.item_id)
            adapter = PlaidProductionAdapter(
                PlaidProductionHttpClient(
                    client_id=plaid_config.client_id,
                    client_secret=plaid_config.client_secret,
                    transport=UrllibJsonTransport(),
                ),
                secrets.resolve(next(iter(access_refs))),
                state=PlaidCursorState(state.cursor, dict(state.records)),
            )
            batch = adapter.read_batch()
            _validate_selected_plaid_accounts(batch, account_ids)
            if not adapter.state.cursor:
                raise TaskBlocked("Plaid webhook sync completed without a durable cursor")
            self.store.persist_plaid_sync_state(
                organization_id=request.organization_id,
                item_id=request.item_id,
                cursor_value=adapter.state.cursor,
                records=adapter.state.records,
            )
        except TaskBlocked:
            raise
        except (ProviderReadError, SecretStoreError, ValueError) as exc:
            raise TaskBlocked("Plaid webhook synchronization could not complete; inspect provider configuration and logs") from exc


class EvidenceStore(Protocol):
    def get_close_run(self, run_id: str):
        ...

    def persist_evidence_batch(self, **kwargs) -> None:
        ...

    def mapping_for_run(self, run_id: str):
        ...

    def connection_secret_ref(self, organization_id: str, provider: str, provider_target: str) -> str | None:
        ...

    def accounting_timezone_for_organization(self, organization_id: str) -> str:
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
            mapping = self.store.mapping_for_run(task.run_id)
            if mapping is None:
                raise TaskBlocked("Google evidence collection requires an accountant-approved close mapping")
            configuration = _mapping_configuration(mapping)
            evidence = configuration.get("evidence")
            if not isinstance(evidence, Mapping):
                raise TaskBlocked("Google evidence scope configuration is incomplete")
            folders = frozenset(str(item) for item in evidence.get("drive_folder_ids", []) if str(item))
            mailbox = str(evidence.get("gmail_mailbox", "")).strip()
            labels = frozenset(str(item) for item in evidence.get("gmail_labels", []) if str(item))
            refresh_token_ref = self.store.connection_secret_ref(run.organization_id, "drive", "workspace")
            if not refresh_token_ref or not folders or not mailbox or not labels:
                raise TaskBlocked("Google evidence scope configuration is incomplete")
            secrets = secret_store_from_environment(self.env)
            access_token_ref = refresh_token_ref.rsplit("/", 1)[0] + "/access-token"
            GoogleOAuthClient(GoogleOAuthConfig.from_environment(self.env), secrets).refresh_access_token(
                refresh_token_ref, access_token_ref
            )
            timezone_lookup = getattr(self.store, "accounting_timezone_for_organization", None)
            accounting_timezone = timezone_lookup(run.organization_id) if callable(timezone_lookup) else "UTC"
            scope = EvidenceScope(
                folders,
                mailbox,
                labels,
                date.fromisoformat(run.period_start),
                date.fromisoformat(run.period_end),
                accounting_timezone,
            )
            transport = UrllibJsonTransport()
            batch = EvidenceCollector(
                GoogleDriveHttpClient(access_token_ref, secrets, transport),
                GmailHttpClient(access_token_ref, secrets, transport),
            ).collect(scope)
            checklist = _evidence_checklist(evidence)
            evaluation = evaluate_checklist(checklist, batch)
            self.store.persist_evidence_batch(run_id=task.run_id, batch=batch, evaluation=evaluation)
            if not evaluation.ready:
                missing = ", ".join(item.requirement_id for item in evaluation.missing)
                raise TaskBlocked(f"evidence checklist is incomplete: {missing}")
        except TaskBlocked:
            raise
        except (EvidencePolicyError, ProviderReadError, GoogleOAuthError, SecretStoreError, ValueError) as exc:
            # The collector intentionally filters ordinary out-of-period and
            # unlabeled results.  A remaining collection/policy failure is a
            # visible close blocker, never an unhandled worker exception.
            raise TaskBlocked("Google evidence collection could not complete; inspect provider configuration and logs") from exc


class ReconciliationMappingGateExecutor:
    """Compatibility adapter for callers that still instantiate the old gate.

    Deployed workers use :class:`DurableReconciliationExecutor` directly. A
    caller without the durable store still fails closed rather than attempting
    a reconciliation from environment variables or browser input.
    """

    def __init__(self, store=None) -> None:
        self.store = store

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "reconcile":
            raise TaskBlocked(f"reconciliation gate cannot run {task.task_key}")
        if self.store is not None:
            DurableReconciliationExecutor(self.store).execute(task)
            return
        raise TaskBlocked(
            "reconciliation requires a versioned accountant-approved bank-to-ledger mapping, "
            "matching tolerances, and permitted account codes"
        )


class DurableReconciliationExecutor:
    """Run reconciliation/reports from frozen normalized records and persist controls.

    No provider read occurs here. The only input is the committed source
    snapshot plus the mapping stored on the close run.
    """

    def __init__(self, store, env: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.env = os.environ if env is None else env

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "reconcile":
            raise TaskBlocked(f"reconciliation executor cannot run {task.task_key}")
        run = self.store.get_close_run(task.run_id)
        if run is None or run.snapshot_id is None:
            raise TaskBlocked("reconciliation requires a committed source snapshot")
        mapping = self.store.mapping_for_run(task.run_id)
        if mapping is None:
            raise TaskBlocked("reconciliation requires an accountant-approved close mapping")
        try:
            configuration = _mapping_configuration(mapping)
            execution = derive_close_execution(
                self.store.snapshot_facts_for_run(task.run_id),
                configuration,
                period_start=date.fromisoformat(run.period_start),
                period_end=date.fromisoformat(run.period_end),
            )
            self.store.persist_close_execution(run_id=task.run_id, execution=execution)
            self._explain_open_exceptions(run, configuration)
            # The generated proposals are deterministic facts tied to the same
            # frozen snapshot. create_review_package is idempotent only when
            # its package hash is identical, which protects retry safety.
            self.store.create_review_package(run_id=task.run_id, proposals=execution.proposals)
            self._archive_close_package(task.run_id, run.organization_id)
        except TaskBlocked:
            raise
        except (CloseExecutionError, ValueError) as exc:
            raise TaskBlocked("persisted source records cannot be safely reconciled; inspect the source contract") from exc

    def _explain_open_exceptions(self, run, configuration: Mapping[str, object]) -> None:
        run_id = run.run_id
        exceptions = self.store.unexplained_exceptions_for_run(run_id)
        if not exceptions:
            return
        try:
            service = GroundedExplanationService(GroqExplanationModel(GroqConfig.from_environment(self.env)))
        except GroqError:
            # Explanations enrich a controller's review; an unavailable model
            # must not convert every already-persisted reconciliation
            # exception into a blocked close.  The underlying exception stays
            # open and visibly records that no AI explanation was available.
            for exception in exceptions:
                self.store.mark_exception_explanation_unavailable(
                    run_id=run_id,
                    exception_id=str(exception["id"]),
                )
            return
        permitted_codes = frozenset(str(item) for item in configuration.get("permitted_journal_account_codes", []))
        for exception in exceptions:
            facts_raw = exception.get("facts", [])
            facts = tuple(
                GroundedFact(str(item["evidence_id"]), str(item["field"]), str(item["value"]))
                for item in facts_raw
                if isinstance(item, Mapping) and all(key in item for key in ("evidence_id", "field", "value"))
            )
            if not facts or len({item.evidence_id for item in facts}) != len(facts):
                self.store.mark_exception_explanation_unavailable(
                    run_id=run_id,
                    exception_id=str(exception["id"]),
                )
                continue
            supported_dates = frozenset(
                date.fromisoformat(item.value)
                for item in facts
                if item.field.endswith(".date") and len(item.value) >= 10
            )
            context = ExplanationContext(
                str(exception["id"]), facts,
                supported_amounts=frozenset({Decimal(str(exception["amount"]))}),
                supported_account_codes=permitted_codes,
                supported_dates=supported_dates,
            )
            try:
                response = service.explain(context)
                audit = service.audit_records[-1]
                self.store.record_exception_explanation(
                    run_id=run_id, exception_id=str(exception["id"]), explanation={
                        "cause": response.cause, "recommendation": response.recommendation,
                        "evidence_ids": list(response.evidence_ids), "uncertainties": list(response.uncertainties),
                        "confidence_label": response.confidence_label, "amounts": list(response.amounts),
                        "account_codes": list(response.account_codes), "dates": list(response.dates),
                    }, model_id=audit.model, prompt_version=audit.prompt_version, schema_version=audit.schema_version,
                    input_hash=audit.input_hash, output_hash=audit.output_hash, validation_status=audit.validation,
                    latency_ms=audit.latency_ms, input_tokens=audit.token_count, output_tokens=audit.token_count,
                )
                self._mark_server_connection(
                    organization_id=run.organization_id,
                    provider="groq",
                    provider_target="grounded-explanations",
                    credential_ref="secret://environment/groq/api-key",
                )
            except Exception as exc:
                audit = service.audit_records[-1] if service.audit_records else None
                self.store.record_exception_explanation(
                    run_id=run_id, exception_id=str(exception["id"]), explanation=None,
                    model_id=getattr(service.model, "model_name", "groq"), prompt_version=service.prompt_version,
                    schema_version=service.schema_version, input_hash=audit.input_hash if audit else "",
                    output_hash=audit.output_hash if audit else None, validation_status="rejected",
                    latency_ms=audit.latency_ms if audit else None, input_tokens=audit.token_count if audit else None,
                    output_tokens=audit.token_count if audit else None,
                )
                # Leave the underlying exception open and its rejected
                # explanation auditable, then continue with the remaining
                # bounded explanations. One transient Groq failure must not
                # stop an otherwise reviewable close package.
                continue

    def _archive_close_package(self, run_id: str, organization_id: str) -> None:
        try:
            config = B2Config.from_environment(self.env)
            client = B2ObjectLockClient(config)
            artifact = client.upload_close_package(run_id=run_id, package=self.store.close_artifact_payload(run_id))
            self.store.record_close_artifact(
                run_id=run_id, object_key=artifact.object_key, content_hash=artifact.content_hash,
                retain_until=artifact.retain_until, provider_file_id=artifact.file_id,
            )
            self._mark_server_connection(
                organization_id=organization_id,
                provider="b2",
                provider_target="compliance-close-archive",
                credential_ref="secret://environment/b2/static-credentials",
            )
        except B2Error as exc:
            raise TaskBlocked("B2 immutable close-package storage is not configured or did not confirm Object Lock") from exc

    def _mark_server_connection(
        self,
        *,
        organization_id: str,
        provider: str,
        provider_target: str,
        credential_ref: str,
    ) -> None:
        upsert = getattr(self.store, "upsert_connection", None)
        if not callable(upsert) or not credential_ref.startswith("secret://"):
            return
        now = datetime.now(timezone.utc)
        upsert(
            connection_health=ConnectionHealth(
                connection_id=f"{provider}:{organization_id}:{provider_target}",
                organization_id=organization_id,
                provider=provider,
                provider_environment="production",
                provider_tenant_or_account_id=provider_target,
                status=ConnectionStatus.HEALTHY,
                last_verified_at=now,
                last_success_at=now,
            ),
            credential_secret_ref=credential_ref,
        )


class XeroDraftActionExecutor:
    """Create only approved Xero DRAFT journals and verify a read-back."""

    def __init__(self, store, env: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.env = os.environ if env is None else env

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "apply_approved_actions":
            raise TaskBlocked(f"Xero action executor cannot run {task.task_key}")
        run = self.store.get_close_run(task.run_id)
        if run is None:
            raise TaskBlocked("approved close run is unavailable")
        try:
            proposals = self.store.approved_xero_proposals_for_run(task.run_id)
            if not proposals:
                return
            configuration = proposals[0].get("configuration")
            if not isinstance(configuration, Mapping):
                raise TaskBlocked("approved close mapping is unavailable")
            configuration = _validate_mapping_configuration(configuration)
            tenant_id = str(configuration["xero_tenant_id"])
            secrets = secret_store_from_environment(self.env)
            config = XeroOAuthConfig.from_environment(self.env)
            refresh_ref = self.store.connection_secret_ref(run.organization_id, "xero", tenant_id)
            if not refresh_ref:
                raise TaskBlocked("approved Xero tenant credentials are unavailable")
            oauth = XeroOAuthClient(replace(config, refresh_token_secret_ref=refresh_ref), secrets)
            client = XeroDraftHttpClient(tenant_id, refresh_ref, secrets, UrllibJsonTransport(), oauth)
            for proposal in proposals:
                if proposal.get("configuration") != configuration:
                    raise TaskBlocked("approved journal proposals do not share one frozen close mapping")
                self._execute_proposal(task.run_id, proposal, client, configuration)
        except TaskBlocked:
            raise
        except (XeroOAuthError, SecretStoreError, ValueError) as exc:
            raise TaskBlocked("Xero DRAFT journal action configuration is invalid") from exc

    def _execute_proposal(
        self,
        run_id: str,
        proposal: Mapping[str, object],
        client: XeroDraftHttpClient,
        configuration: Mapping[str, object],
    ) -> None:
        raw_lines = proposal.get("lines", [])
        if not isinstance(raw_lines, list) or any(not isinstance(item, Mapping) for item in raw_lines):
            raise TaskBlocked("approved journal proposal has invalid lines")
        lines = tuple(
            (str(item["account_code"]), str(item["debit"]), str(item["credit"]), tuple(str(value) for value in item.get("evidence_ids", [])))
            for item in raw_lines
        )
        if not lines:
            raise TaskBlocked("approved journal proposal has no balanced lines")
        permitted_codes = frozenset(str(item) for item in configuration.get("permitted_journal_account_codes", []))
        if not permitted_codes or any(account_code not in permitted_codes for account_code, _, _, _ in lines):
            raise TaskBlocked("approved journal proposal contains an account code outside the frozen mapping")
        intent_hash = sha256(f"{proposal['proposal_hash']}|{proposal['journal_date']}|{lines}".encode()).hexdigest()
        marker = f"AOSMJv1/{run_id[:8]}/{str(proposal['proposal_hash'])[:16]}/{intent_hash[:12]}"
        narration = f"{marker} | {proposal['narration']}"
        request_hash = sha256(f"{proposal['proposal_hash']}|{narration}|DRAFT|{lines}".encode()).hexdigest()
        action = self.store.ensure_action_execution(
            run_id=run_id, approval_id=str(proposal["approval_id"]), provider="xero",
            operation="create_draft_manual_journal", idempotency_key=f"{run_id}:xero:{proposal['proposal_hash']}",
            request_hash=request_hash, marker=marker,
        )
        if action["status"] in {"succeeded", "reconciled"}:
            return
        request = XeroDraftRequest(str(action["id"]), str(proposal["proposal_id"]), str(proposal["proposal_hash"]), marker,
                                   narration, str(proposal["journal_date"]), lines, request_hash=request_hash)
        self.store.update_action_execution(action_id=str(action["id"]), status="started")
        try:
            found = client.search_manual_journals(marker)
        except Exception as exc:
            self.store.update_action_execution(action_id=str(action["id"]), status="outcome_unknown")
            raise TaskBlocked("Xero DRAFT action outcome is unknown; use action recovery before retrying") from exc
        if len(found) > 1:
            self.store.update_action_execution(action_id=str(action["id"]), status="failed")
            raise TaskBlocked("Xero DRAFT action marker returned multiple journals; manual recovery is required")
        try:
            record = found[0] if found else client.create_draft_manual_journal(request)
            read_back = client.get_manual_journal(record.journal_id)
        except Exception as exc:
            try:
                after = client.search_manual_journals(marker)
            except Exception:
                after = None
            if after is None or len(after) != 1:
                self.store.update_action_execution(action_id=str(action["id"]), status="outcome_unknown")
                raise TaskBlocked("Xero DRAFT action outcome is unknown; use action recovery before retrying") from exc
            read_back = after[0]
        if not self._matches(request, read_back):
            self.store.update_action_execution(action_id=str(action["id"]), status="failed")
            raise TaskBlocked("Xero read-back does not match the approved DRAFT journal")
        self.store.update_action_execution(
            action_id=str(action["id"]), status="succeeded", provider_object_id=read_back.journal_id,
            proposal_id=str(proposal["proposal_id"]), package_hash=str(proposal["package_hash"]), proposal_hash=str(proposal["proposal_hash"]),
        )

    @staticmethod
    def _matches(request: XeroDraftRequest, record) -> bool:
        expected_lines = tuple((code, Decimal(debit), Decimal(credit)) for code, debit, credit, _ in request.lines)
        observed_lines = tuple((code, Decimal(debit), Decimal(credit)) for code, debit, credit, _ in record.lines)
        return record.status == "DRAFT" and record.narration == request.narration and record.journal_date == request.journal_date and observed_lines == expected_lines


class GmailRecoveryActionExecutor:
    """Draft, send, and recover only approved, allowlisted exception requests."""

    def __init__(self, store, env: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.env = os.environ if env is None else env

    def execute(self, task: PersistedTask) -> None:
        if task.task_key != "send_recovery_request":
            raise TaskBlocked(f"Gmail recovery executor cannot run {task.task_key}")
        run = self.store.get_close_run(task.run_id)
        if run is None:
            raise TaskBlocked("close run is unavailable for Gmail recovery")
        try:
            secrets = secret_store_from_environment(self.env)
            refresh_ref = self.store.connection_secret_ref(run.organization_id, "gmail", "workspace")
            if not refresh_ref:
                raise TaskBlocked("Google Gmail credentials are unavailable")
            access_ref = refresh_ref.rsplit("/", 1)[0] + "/access-token"
            GoogleOAuthClient(GoogleOAuthConfig.from_environment(self.env), secrets).refresh_access_token(refresh_ref, access_ref)
            client = GmailHttpClient(access_ref, secrets, UrllibJsonTransport())
            for action in self.store.prepared_recovery_email_actions(task.run_id):
                self._send(action, client)
        except TaskBlocked:
            raise
        except (GoogleOAuthError, SecretStoreError, ValueError) as exc:
            raise TaskBlocked("Gmail recovery action configuration is invalid") from exc

    def _send(self, action: Mapping[str, object], client: GmailHttpClient) -> None:
        configuration = action.get("configuration")
        evidence = configuration.get("evidence") if isinstance(configuration, Mapping) else None
        recipients = frozenset(str(item).lower() for item in evidence.get("allowed_recipients", [])) if isinstance(evidence, Mapping) else frozenset()
        request = EmailRequest(
            str(action["recipient"]),
            EmailTemplate("exception-recovery-v1", "AccountingOS: close exception evidence request",
                          f"Please provide evidence for {action['control_code']}. Required follow-up: {action['remediation']}"),
            (str(action["exception_id"]),),
        )
        try:
            count_lookup = getattr(self.store, "recovery_email_counts", None)
            if not callable(count_lookup):
                raise TaskBlocked("durable recovery-email rate-limit accounting is unavailable")
            run_count, recipient_count = count_lookup(
                run_id=str(action["run_id"]),
                recipient=request.recipient,
                excluding_action_id=str(action["action_id"]),
            )
            EmailPolicy(recipients, frozenset(), frozenset({"exception-recovery-v1"})).authorize(
                request,
                run_count=run_count,
                recipient_count=recipient_count,
            )
            sent = client.search_sent_by_marker(str(action["marker"]))
            if sent is None:
                self.store.update_action_execution(action_id=str(action["action_id"]), status="outcome_unknown")
                raise TaskBlocked("Gmail send outcome is unknown; use action recovery before retrying")
            if len(sent) > 1:
                self.store.update_action_execution(action_id=str(action["action_id"]), status="failed")
                raise TaskBlocked("Gmail send marker returned multiple messages; manual recovery is required")
            if sent:
                self.store.update_action_execution(action_id=str(action["action_id"]), status="succeeded", provider_object_id=sent[0].message_id)
                return
            self.store.update_action_execution(action_id=str(action["action_id"]), status="started")
            draft = client.create_request_draft(request.recipient, request.template.subject, request.template.body, str(action["marker"]))
            result = client.send_approved_request(draft.draft_id)
            self.store.update_action_execution(action_id=str(action["action_id"]), status="succeeded", provider_object_id=result.message_id)
        except TaskBlocked:
            raise
        except EvidencePolicyError as exc:
            self.store.update_action_execution(action_id=str(action["action_id"]), status="failed")
            raise TaskBlocked("Gmail recovery action violates the approved evidence policy") from exc
        except Exception as exc:
            try:
                recovered = client.search_sent_by_marker(str(action["marker"]))
            except Exception:
                recovered = None
            if recovered is not None and len(recovered) == 1:
                self.store.update_action_execution(action_id=str(action["action_id"]), status="reconciled", provider_object_id=recovered[0].message_id)
                return
            self.store.update_action_execution(action_id=str(action["action_id"]), status="outcome_unknown")
            raise TaskBlocked("Gmail send outcome is unknown; use action recovery before retrying") from exc


def _mapping_configuration(mapping: PersistedCloseMapping) -> Mapping[str, object]:
    return _validate_mapping_configuration(mapping.configuration)


def _evidence_checklist(evidence: Mapping[str, object]) -> ChecklistVersion:
    """Rebuild the exact versioned checklist captured in the close mapping."""
    # Existing approved mappings predate the explicit checklist field. Treat
    # them as the same safe default the mapping model now serializes, rather
    # than silently accepting an empty evidence batch.
    raw = evidence.get(
        "checklist",
        {
            "id": "close-evidence-v1",
            "version": 1,
            "requirements": [{
                "requirement_id": "scoped-evidence",
                "description": "At least one scoped close evidence item is required.",
                "required_tags": [],
                "allowed_kinds": ["document", "email"],
            }],
        },
    )
    if not isinstance(raw, Mapping):
        raise TaskBlocked("persisted close mapping has no evidence checklist")
    checklist_id = raw.get("id")
    version = raw.get("version")
    requirements = raw.get("requirements")
    if (
        not isinstance(checklist_id, str)
        or not checklist_id
        or not isinstance(version, int)
        or not isinstance(requirements, list)
    ):
        raise TaskBlocked("persisted evidence checklist is invalid")
    parsed: list[ChecklistRequirement] = []
    for item in requirements:
        if not isinstance(item, Mapping):
            raise TaskBlocked("persisted evidence checklist requirement is invalid")
        requirement_id = item.get("requirement_id")
        description = item.get("description")
        tags = item.get("required_tags", [])
        kinds = item.get("allowed_kinds", [])
        if (
            not isinstance(requirement_id, str)
            or not requirement_id
            or not isinstance(description, str)
            or not description
            or not isinstance(tags, list)
            or not isinstance(kinds, list)
            or any(not isinstance(value, str) or not value for value in (*tags, *kinds))
        ):
            raise TaskBlocked("persisted evidence checklist requirement is invalid")
        parsed.append(ChecklistRequirement(requirement_id, description, frozenset(tags), frozenset(kinds)))
    try:
        return ChecklistVersion(checklist_id, version, tuple(parsed))
    except EvidencePolicyError as exc:
        raise TaskBlocked("persisted evidence checklist is invalid") from exc


def _validate_mapping_configuration(configuration: object) -> Mapping[str, object]:
    if not isinstance(configuration, Mapping):
        raise TaskBlocked("persisted close mapping is invalid")
    tenant_id = configuration.get("xero_tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise TaskBlocked("persisted close mapping has no Xero tenant")
    _mapping_account_ids(configuration)
    return configuration


def _mapping_account_ids(configuration: Mapping[str, object]) -> frozenset[str]:
    raw = configuration.get("bank_mappings")
    if not isinstance(raw, list):
        raise TaskBlocked("persisted close mapping has no bank-to-ledger accounts")
    accounts = frozenset(
        str(item.get("plaid_account_id", "")).strip()
        for item in raw
        if isinstance(item, Mapping) and str(item.get("plaid_account_id", "")).strip()
    )
    if not accounts or len(accounts) != len(raw):
        raise TaskBlocked("persisted close mapping has invalid bank-to-ledger accounts")
    return accounts
