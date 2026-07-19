"""Server-side Supabase Postgres configuration and persistence boundary.

The application talks to Supabase through its Postgres connection, not through
browser-exposed table access. The repository methods keep transaction and
idempotency rules close to the database while the domain objects remain pure.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from json import dumps, loads
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import parse_qs, urlparse

from .connections import ConnectionHealth, ConnectionStatus
from .domain import CloseRun, JournalProposal, PolicyError, SourceBatch, SourceSnapshot
from .evidence import EvidenceBatch
from .security import OAuthTransaction


class SupabaseConfigError(PolicyError):
    """Raised when the backend cannot safely connect to Supabase Postgres."""


@dataclass(frozen=True)
class SupabaseDatabaseConfig:
    database_url: str
    connect_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        parsed = urlparse(self.database_url)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise SupabaseConfigError("SUPABASE_DB_URL must be a PostgreSQL URL")
        if "replace-with" in self.database_url:
            raise SupabaseConfigError("SUPABASE_DB_URL must not use a placeholder value")
        if self.connect_timeout_seconds < 1:
            raise SupabaseConfigError("Supabase connection timeout must be positive")
        query = parse_qs(parsed.query)
        sslmode = query.get("sslmode", [""])[0]
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise SupabaseConfigError("Supabase Postgres connections must require TLS")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "SupabaseDatabaseConfig":
        values = os.environ if env is None else env
        database_url = values.get("SUPABASE_DB_URL", "").strip()
        if not database_url:
            raise SupabaseConfigError("SUPABASE_DB_URL is required by the backend")
        if any(key.startswith("NEXT_PUBLIC_") for key in values if "SERVICE_ROLE" in key):
            raise SupabaseConfigError("Supabase service-role credentials cannot be public")
        return cls(database_url, int(values.get("SUPABASE_DB_CONNECT_TIMEOUT", "10")))


@dataclass(frozen=True)
class OrganizationSummary:
    organization_id: str
    name: str
    role: str


@dataclass(frozen=True)
class PersistedCloseRun:
    run_id: str
    organization_id: str
    period_start: str
    period_end: str
    state: str
    deployment_mode: str
    data_class: str
    snapshot_id: str | None
    package_hash: str | None


@dataclass(frozen=True)
class PersistedConnection:
    connection_id: str
    organization_id: str
    provider: str
    provider_environment: str
    provider_tenant_or_account_id: str
    status: str
    granted_scopes: tuple[str, ...]
    last_verified_at: datetime | None
    last_success_at: datetime | None
    consent_expires_at: datetime | None
    remediation: str | None


@dataclass(frozen=True)
class PersistedTask:
    task_id: str
    run_id: str
    task_key: str
    state: str
    attempt: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersistedTaskEvent:
    event_id: int
    organization_id: str
    run_id: str
    task_id: str | None
    event_type: str
    payload: Mapping[str, object]
    created_at: datetime


@dataclass(frozen=True)
class PersistedReviewPackage:
    package_id: str
    organization_id: str
    run_id: str
    snapshot_id: str
    package_hash: str
    status: str
    summary: Mapping[str, object]
    frozen_at: datetime | None


DEFAULT_CLOSE_TASKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("preflight", ()),
    ("synchronize_sources", ("preflight",)),
    ("collect_evidence", ("synchronize_sources",)),
    ("reconcile", ("collect_evidence",)),
)


class Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] | None = None) -> Any:
        ...

    def fetchone(self) -> Any:
        ...

    def fetchall(self) -> list[Any]:
        ...


class Connection(Protocol):
    def cursor(self) -> Cursor:
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...


def connect(config: SupabaseDatabaseConfig):
    """Open a TLS Postgres connection; import psycopg only when used."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised in deployment
        raise SupabaseConfigError("install psycopg[binary] to connect to Supabase") from exc
    return psycopg.connect(config.database_url, connect_timeout=config.connect_timeout_seconds)


@contextmanager
def transaction(connection: Connection) -> Iterator[Cursor]:
    cursor = connection.cursor()
    try:
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        close = getattr(cursor, "close", None)
        if close is not None:
            close()


class SupabaseRepository:
    """Minimal transactional repository used by the worker and API services."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def insert_close_run(self, run: CloseRun) -> None:
        with transaction(self.connection) as cursor:
            cursor.execute(
                """
                insert into workflow.close_runs
                    (id, organization_id, deployment_id, period_start, period_end,
                     deployment_mode, data_class, market, currency, state)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run.run_id,
                    run.organization_id,
                    run.deployment.deployment_id,
                    run.period_start,
                    run.period_end,
                    run.deployment.mode,
                    run.deployment.data_class,
                    run.deployment.market,
                    run.deployment.currency,
                    run.state.value,
                ),
            )

    def update_run_state(self, run: CloseRun) -> None:
        with transaction(self.connection) as cursor:
            cursor.execute(
                "update workflow.close_runs set state = %s, snapshot_id = %s, package_hash = %s, updated_at = %s where id = %s",
                (
                    run.state.value,
                    run.snapshot.snapshot_id if run.snapshot else None,
                    run.package_hash,
                    datetime.now(timezone.utc),
                    run.run_id,
                ),
            )

    def insert_source_batch(self, organization_id: str, run_id: str, batch: SourceBatch) -> None:
        with transaction(self.connection) as cursor:
            cursor.execute(
                """
                insert into normalized.source_batches
                    (id, organization_id, run_id, provider, provider_environment,
                     watermark, completed_at, complete, warnings)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    batch.batch_id,
                    organization_id,
                    run_id,
                    batch.provider,
                    batch.provider_environment,
                    batch.watermark,
                    batch.completed_at,
                    batch.complete,
                    dumps(batch.warnings),
                ),
            )

    def insert_snapshot(self, organization_id: str, run_id: str, snapshot: SourceSnapshot) -> None:
        with transaction(self.connection) as cursor:
            cursor.execute(
                """
                insert into normalized.source_snapshots
                    (id, organization_id, run_id, deployment_id, deployment_mode,
                     data_class, snapshot_cutoff_at, source_batch_ids, status)
                values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'complete')
                """,
                (
                    snapshot.snapshot_id,
                    organization_id,
                    run_id,
                    snapshot.deployment_id,
                    snapshot.mode,
                    snapshot.data_class,
                    snapshot.cutoff_at,
                    dumps(snapshot.source_batch_ids),
                ),
            )
            for record in snapshot.records:
                cursor.execute(
                    """
                    insert into normalized.snapshot_records
                        (snapshot_id, normalized_record_version_id, source_batch_id,
                         provider, provider_record_id, content_hash)
                    values (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        snapshot.snapshot_id,
                        record.record_version_id,
                        record.source_batch_id,
                        record.provider,
                        record.provider_record_id,
                        record.content_hash,
                    ),
                )

    def append_audit_event(
        self,
        *,
        organization_id: str | None,
        run_id: str | None,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        with transaction(self.connection) as cursor:
            cursor.execute(
                "insert into audit.events (organization_id, run_id, event_type, payload_json) values (%s, %s, %s, %s::jsonb)",
                (organization_id, run_id, event_type, dumps(payload, default=str)),
            )

    def claim_ready_task(self, owner: str, *, lease_seconds: int = 60) -> Mapping[str, object] | None:
        if not owner or lease_seconds < 1:
            raise SupabaseConfigError("task claims require an owner and positive lease")
        with transaction(self.connection) as cursor:
            cursor.execute(
                """
                select id, run_id, task_key, attempt
                from workflow.tasks
                where state = 'ready'
                  and (lease_expires_at is null or lease_expires_at < now())
                order by created_at
                for update skip locked
                limit 1
                """
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                update workflow.tasks
                set state = 'running', lease_owner = %s,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    attempt = attempt + 1, updated_at = now()
                where id = %s
                """,
                (owner, lease_seconds, row[0]),
            )
            return {"id": row[0], "run_id": row[1], "task_key": row[2], "attempt": row[3] + 1}


class SupabaseWorkflowStore:
    """Durable API-facing workflow store for private Supabase schemas.

    Every public method opens a short-lived server-side TLS connection. Browser
    clients never receive these credentials and organization authorization is
    checked against ``workflow.organization_users`` before any read/write.
    """

    def __init__(self, config: SupabaseDatabaseConfig) -> None:
        self.config = config

    def membership_role(self, organization_id: str, issuer: str, subject: str) -> str | None:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select role from workflow.organization_users
                    where organization_id = %s and identity_issuer = %s and identity_subject = %s
                    """,
                    (organization_id, issuer, subject),
                )
                row = cursor.fetchone()
            return str(row[0]) if row is not None else None
        finally:
            self._close(connection)

    def organizations_for_user(self, issuer: str, subject: str) -> tuple[OrganizationSummary, ...]:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select o.id, o.name, u.role
                    from workflow.organization_users u
                    join workflow.organizations o on o.id = u.organization_id
                    where u.identity_issuer = %s and u.identity_subject = %s and o.status = 'active'
                    order by o.name, o.id
                    """,
                    (issuer, subject),
                )
                rows = cursor.fetchall()
            return tuple(OrganizationSummary(str(row[0]), str(row[1]), str(row[2])) for row in rows)
        finally:
            self._close(connection)

    def bootstrap_organization(
        self,
        *,
        organization_id: str,
        organization_name: str,
        deployment: "DeploymentConfig",
        issuer: str,
        subject: str,
    ) -> OrganizationSummary:
        """Create the one configured demo organization and its controller membership.

        The caller is allowlisted by the API before this method runs. Repeated
        calls are idempotent and never change an existing deployment boundary.
        """
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    insert into workflow.deployments
                        (id, mode, data_class, market, currency, controller_subject)
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (id) do nothing
                    """,
                    (
                        deployment.deployment_id,
                        deployment.mode,
                        deployment.data_class,
                        deployment.market,
                        deployment.currency,
                        subject,
                    ),
                )
                cursor.execute(
                    """
                    insert into workflow.organizations
                        (id, deployment_id, name, market, functional_currency, accounting_timezone)
                    values (%s, %s, %s, %s, %s, 'UTC')
                    on conflict (id) do nothing
                    """,
                    (
                        organization_id,
                        deployment.deployment_id,
                        organization_name,
                        deployment.market,
                        deployment.currency,
                    ),
                )
                cursor.execute(
                    """
                    insert into workflow.organization_users
                        (organization_id, identity_issuer, identity_subject, role)
                    values (%s, %s, %s, 'controller')
                    on conflict (organization_id, identity_issuer, identity_subject)
                    do update set role = excluded.role
                    returning role
                    """,
                    (organization_id, issuer, subject),
                )
                row = cursor.fetchone()
            return OrganizationSummary(organization_id, organization_name, str(row[0]))
        finally:
            self._close(connection)

    def create_close_run(
        self,
        *,
        organization_id: str,
        deployment: "DeploymentConfig",
        period_start: str,
        period_end: str,
        idempotency_key: str,
    ) -> PersistedCloseRun:
        if not idempotency_key or len(idempotency_key) > 200:
            raise SupabaseConfigError("Idempotency-Key is required and must be at most 200 characters")
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    insert into workflow.close_runs
                        (organization_id, deployment_id, period_start, period_end,
                         deployment_mode, data_class, market, currency, state, request_key)
                    values (%s, %s, %s::date, %s::date, %s, %s, %s, %s, 'synchronizing', %s)
                    on conflict (organization_id, request_key) do update
                    set updated_at = workflow.close_runs.updated_at
                    returning id::text, organization_id, period_start::text, period_end::text,
                              state, deployment_mode, data_class, snapshot_id::text, package_hash
                    """,
                    (
                        organization_id,
                        deployment.deployment_id,
                        period_start,
                        period_end,
                        deployment.mode,
                        deployment.data_class,
                        deployment.market,
                        deployment.currency,
                        idempotency_key,
                    ),
                )
                row = cursor.fetchone()
                self._ensure_default_tasks(cursor, str(row[0]), str(row[1]))
            return self._run_from_row(row)
        finally:
            self._close(connection)

    def tasks_for_run(self, run_id: str) -> tuple[PersistedTask, ...]:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select t.id::text, t.run_id::text, t.task_key, t.state, t.attempt,
                           t.lease_owner, t.lease_expires_at, t.last_error,
                           coalesce(array_agg(d.task_key order by d.task_key)
                                    filter (where d.task_key is not null), array[]::text[])
                    from workflow.tasks t
                    left join workflow.task_dependencies td on td.task_id = t.id
                    left join workflow.tasks d on d.id = td.depends_on_task_id
                    where t.run_id = %s::uuid
                    group by t.id
                    order by t.created_at, t.task_key
                    """,
                    (run_id,),
                )
                rows = cursor.fetchall()
            return tuple(self._task_from_row(row) for row in rows)
        finally:
            self._close(connection)

    def events_for_run(
        self,
        run_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 100,
    ) -> tuple[PersistedTaskEvent, ...]:
        if after_event_id < 0 or not 1 <= limit <= 500:
            raise SupabaseConfigError("event cursor and limit are invalid")
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select id, organization_id, run_id::text, task_id::text,
                           event_type, payload_json, created_at
                    from workflow.task_events
                    where run_id = %s::uuid and id > %s
                    order by id
                    limit %s
                    """,
                    (run_id, after_event_id, limit),
                )
                rows = cursor.fetchall()
            return tuple(self._event_from_row(row) for row in rows)
        finally:
            self._close(connection)

    def claim_next_task(self, worker_id: str, *, lease_seconds: int = 60) -> PersistedTask | None:
        if not worker_id or not 1 <= lease_seconds <= 900:
            raise SupabaseConfigError("worker id and lease duration are invalid")
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select t.id::text, t.run_id::text, t.task_key, t.state, t.attempt,
                           t.lease_owner, t.lease_expires_at, t.last_error,
                           r.organization_id
                    from workflow.tasks t
                    join workflow.close_runs r on r.id = t.run_id
                    where t.state = 'ready'
                      and (t.lease_expires_at is null or t.lease_expires_at < now())
                    order by t.created_at, t.id
                    for update skip locked
                    limit 1
                    """
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                cursor.execute(
                    """
                    update workflow.tasks
                    set state = 'running', lease_owner = %s,
                        lease_expires_at = now() + (%s * interval '1 second'),
                        attempt = attempt + 1, updated_at = now()
                    where id = %s::uuid
                    returning id::text, run_id::text, task_key, state, attempt,
                              lease_owner, lease_expires_at, last_error
                    """,
                    (worker_id, lease_seconds, row[0]),
                )
                task_row = cursor.fetchone()
                self._record_task_event(
                    cursor,
                    organization_id=str(row[8]),
                    run_id=str(row[1]),
                    task_id=str(row[0]),
                    event_type="task_claimed",
                    payload={"task_key": str(row[2]), "worker_id": worker_id, "attempt": task_row[4]},
                )
            return self._task_from_row(task_row)
        finally:
            self._close(connection)

    def complete_task(self, task: PersistedTask, worker_id: str) -> None:
        self._finish_task(task, worker_id, state="succeeded", error=None)

    def block_task(self, task: PersistedTask, worker_id: str, error: str) -> None:
        if not error:
            raise SupabaseConfigError("a blocked task needs an error message")
        self._finish_task(task, worker_id, state="blocked", error=error)

    def retry_run(self, run_id: str) -> PersistedCloseRun:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    "select organization_id from workflow.close_runs where id = %s::uuid for update",
                    (run_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise SupabaseConfigError("close run does not exist")
                cursor.execute(
                    """
                    update workflow.tasks candidate
                    set state = 'ready', last_error = null, lease_owner = null,
                        lease_expires_at = null, updated_at = now()
                    where candidate.run_id = %s::uuid and candidate.state in ('blocked', 'failed')
                      and not exists (
                        select 1
                        from workflow.task_dependencies dependency
                        join workflow.tasks prerequisite on prerequisite.id = dependency.depends_on_task_id
                        where dependency.task_id = candidate.id and prerequisite.state <> 'succeeded'
                      )
                    """,
                    (run_id,),
                )
                cursor.execute(
                    """
                    update workflow.close_runs set state = 'synchronizing', updated_at = now()
                    where id = %s::uuid
                    returning id::text, organization_id, period_start::text, period_end::text,
                              state, deployment_mode, data_class, snapshot_id::text, package_hash
                    """,
                    (run_id,),
                )
                run_row = cursor.fetchone()
                self._record_task_event(
                    cursor,
                    organization_id=str(row[0]),
                    run_id=run_id,
                    task_id=None,
                    event_type="run_retry_requested",
                    payload={},
                )
            return self._run_from_row(run_row)
        finally:
            self._close(connection)

    def cancel_run(self, run_id: str) -> PersistedCloseRun:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    "select organization_id from workflow.close_runs where id = %s::uuid for update",
                    (run_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise SupabaseConfigError("close run does not exist")
                cursor.execute(
                    """
                    update workflow.tasks
                    set state = 'cancelled', lease_owner = null, lease_expires_at = null, updated_at = now()
                    where run_id = %s::uuid and state in ('pending', 'ready')
                    """,
                    (run_id,),
                )
                cursor.execute(
                    """
                    update workflow.close_runs set state = 'cancelled', updated_at = now()
                    where id = %s::uuid
                    returning id::text, organization_id, period_start::text, period_end::text,
                              state, deployment_mode, data_class, snapshot_id::text, package_hash
                    """,
                    (run_id,),
                )
                run_row = cursor.fetchone()
                self._record_task_event(
                    cursor,
                    organization_id=str(row[0]),
                    run_id=run_id,
                    task_id=None,
                    event_type="run_cancelled",
                    payload={},
                )
            return self._run_from_row(run_row)
        finally:
            self._close(connection)

    def create_review_package(
        self,
        *,
        run_id: str,
        proposals: Sequence[JournalProposal],
    ) -> PersistedReviewPackage:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select organization_id, snapshot_id::text, state
                    from workflow.close_runs where id = %s::uuid for update
                    """,
                    (run_id,),
                )
                run = cursor.fetchone()
                if run is None:
                    raise SupabaseConfigError("close run does not exist")
                organization_id, snapshot_id, state = str(run[0]), run[1], str(run[2])
                if state != "running" or snapshot_id is None:
                    raise SupabaseConfigError("a complete source snapshot is required before review")
                package_hash = sha256(
                    f"{snapshot_id}|{'|'.join(proposal.proposal_hash for proposal in proposals)}".encode()
                ).hexdigest()
                summary = {"proposal_count": len(proposals), "proposal_hashes": [item.proposal_hash for item in proposals]}
                cursor.execute(
                    """
                    insert into workflow.review_packages
                        (organization_id, run_id, snapshot_id, package_hash, status, summary_json)
                    values (%s, %s::uuid, %s::uuid, %s, 'review_frozen', %s::jsonb)
                    on conflict (run_id) do update
                    set package_hash = workflow.review_packages.package_hash
                    where workflow.review_packages.package_hash = excluded.package_hash
                    returning id::text, organization_id, run_id::text, snapshot_id::text,
                              package_hash, status, summary_json, frozen_at
                    """,
                    (organization_id, run_id, snapshot_id, package_hash, dumps(summary)),
                )
                package_row = cursor.fetchone()
                if package_row is None:
                    raise SupabaseConfigError("review package is already frozen with different content")
                package_id = str(package_row[0])
                for proposal in proposals:
                    cursor.execute(
                        """
                        insert into workflow.journal_proposals
                            (id, organization_id, run_id, review_package_id, journal_date,
                             narration, proposal_hash, status)
                        values (%s, %s, %s::uuid, %s::uuid, %s::date, %s, %s, 'proposed')
                        on conflict (id) do nothing
                        """,
                        (
                            proposal.proposal_id,
                            organization_id,
                            run_id,
                            package_id,
                            proposal.journal_date,
                            proposal.display_narration,
                            proposal.proposal_hash,
                        ),
                    )
                    for line_number, line in enumerate(proposal.lines, start=1):
                        cursor.execute(
                            """
                            insert into workflow.journal_proposal_lines
                                (proposal_id, line_number, account_code, debit, credit, evidence_ids)
                            values (%s, %s, %s, %s, %s, %s::jsonb)
                            on conflict (proposal_id, line_number) do nothing
                            """,
                            (
                                proposal.proposal_id,
                                line_number,
                                line.account_code,
                                line.debit,
                                line.credit,
                                dumps(list(line.evidence_ids)),
                            ),
                        )
                cursor.execute(
                    """
                    update workflow.close_runs
                    set state = 'awaiting_approval', package_hash = %s, updated_at = now()
                    where id = %s::uuid
                    """,
                    (package_hash, run_id),
                )
                self._record_task_event(
                    cursor,
                    organization_id=organization_id,
                    run_id=run_id,
                    task_id=None,
                    event_type="review_package_frozen",
                    payload={"package_hash": package_hash, "proposal_count": len(proposals)},
                )
            return self._review_package_from_row(package_row)
        finally:
            self._close(connection)

    def approve_review_package(
        self,
        *,
        run_id: str,
        package_hash: str,
        actor_subject: str,
    ) -> PersistedCloseRun:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select r.organization_id, r.snapshot_id::text, p.id::text
                    from workflow.close_runs r
                    join workflow.review_packages p on p.run_id = r.id
                    where r.id = %s::uuid and r.state = 'awaiting_approval' and p.package_hash = %s
                    for update
                    """,
                    (run_id, package_hash),
                )
                row = cursor.fetchone()
                if row is None:
                    raise SupabaseConfigError("approval must reference the current frozen review package")
                organization_id, snapshot_id, package_id = str(row[0]), str(row[1]), str(row[2])
                snapshot_hash = sha256(snapshot_id.encode()).hexdigest()
                cursor.execute(
                    """
                    insert into workflow.approvals
                        (run_id, package_hash, snapshot_hash, actor_subject, decision)
                    values (%s::uuid, %s, %s, %s, 'approved')
                    """,
                    (run_id, package_hash, snapshot_hash, actor_subject),
                )
                cursor.execute(
                    "select count(*) from workflow.journal_proposals where review_package_id = %s::uuid",
                    (package_id,),
                )
                proposal_count = int(cursor.fetchone()[0])
                if proposal_count:
                    cursor.execute(
                        "update workflow.journal_proposals set status = 'approved' where review_package_id = %s::uuid",
                        (package_id,),
                    )
                next_state = "applying_approved_actions" if proposal_count else "approved"
                cursor.execute(
                    """
                    update workflow.close_runs set state = %s, updated_at = now()
                    where id = %s::uuid
                    returning id::text, organization_id, period_start::text, period_end::text,
                              state, deployment_mode, data_class, snapshot_id::text, package_hash
                    """,
                    (next_state, run_id),
                )
                run_row = cursor.fetchone()
                self._record_task_event(
                    cursor,
                    organization_id=organization_id,
                    run_id=run_id,
                    task_id=None,
                    event_type="review_package_approved",
                    payload={"package_hash": package_hash, "proposal_count": proposal_count},
                )
            return self._run_from_row(run_row)
        finally:
            self._close(connection)

    def persist_source_snapshot(
        self,
        *,
        run_id: str,
        batches: Sequence[SourceBatch],
        snapshot: SourceSnapshot,
        provider_identities: Mapping[str, str],
    ) -> PersistedCloseRun:
        """Commit source batches, normalized facts, raw facts, and one snapshot atomically."""
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select organization_id, deployment_id, deployment_mode, data_class, state
                    from workflow.close_runs where id = %s::uuid for update
                    """,
                    (run_id,),
                )
                run = cursor.fetchone()
                if run is None:
                    raise SupabaseConfigError("close run does not exist")
                organization_id, deployment_id, mode, data_class, state = (
                    str(run[0]), str(run[1]), str(run[2]), str(run[3]), str(run[4])
                )
                if state != "synchronizing":
                    raise SupabaseConfigError("source snapshots require a synchronizing close run")
                if (snapshot.deployment_id, snapshot.mode, snapshot.data_class) != (
                    deployment_id,
                    mode,
                    data_class,
                ):
                    raise SupabaseConfigError("snapshot deployment does not match its close run")
                if (mode, data_class) == ("production", "live"):
                    expected_environments = {"xero": "production", "plaid": "production"}
                    raw_xero_table = "raw_xero.records"
                    raw_bank_table = "raw_bank_us.records"
                elif (mode, data_class) == ("demo", "synthetic"):
                    expected_environments = {"xero": "demo", "plaid": "sandbox"}
                    raw_xero_table = "raw_xero_demo.records"
                    raw_bank_table = "raw_bank_demo.records"
                else:
                    raise SupabaseConfigError("close run has an invalid deployment/data boundary")
                for batch in batches:
                    expected_environment = expected_environments.get(batch.provider)
                    if expected_environment is None or batch.provider_environment != expected_environment:
                        raise SupabaseConfigError("source batch environment does not match its close deployment")
                    cursor.execute(
                        """
                        insert into normalized.source_batches
                            (id, organization_id, run_id, provider, provider_environment,
                             watermark, completed_at, complete, warnings)
                        values (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            batch.batch_id,
                            organization_id,
                            run_id,
                            batch.provider,
                            batch.provider_environment,
                            batch.watermark,
                            batch.completed_at,
                            batch.complete,
                            dumps(list(batch.warnings)),
                        ),
                    )
                    identity = provider_identities.get(batch.provider, "")
                    if not identity:
                        raise SupabaseConfigError(f"{batch.provider} provider identity is required")
                    for record in batch.record_versions:
                        payload = loads(record.payload_json)
                        if not isinstance(payload, Mapping):
                            raise SupabaseConfigError("normalized provider payload must be an object")
                        cursor.execute(
                            """
                            insert into normalized.record_versions
                                (version_id, source_batch_id, provider, provider_record_id,
                                 content_hash, payload_json, observed_at, currency, accounting_date)
                            values (%s, %s::uuid, %s, %s, %s, %s::jsonb, %s, %s, %s::date)
                            """,
                            (
                                record.version_id,
                                batch.batch_id,
                                record.provider,
                                record.provider_record_id,
                                record.content_hash,
                                record.payload_json,
                                record.observed_at,
                                record.currency,
                                record.accounting_date,
                            ),
                        )
                        if batch.provider == "xero":
                            cursor.execute(
                                f"""
                                insert into {raw_xero_table}
                                    (organization_id, run_id, source_batch_id, tenant_id,
                                     provider_record_id, payload_json, content_hash, observed_at, page_number)
                                values (%s, %s::uuid, %s::uuid, %s, %s, %s::jsonb, %s, %s, 1)
                                on conflict do nothing
                                """,
                                (
                                    organization_id,
                                    run_id,
                                    batch.batch_id,
                                    identity,
                                    record.provider_record_id,
                                    record.payload_json,
                                    record.content_hash,
                                    record.observed_at,
                                ),
                            )
                        elif batch.provider == "plaid":
                            account_id = payload.get("account_id")
                            if not isinstance(account_id, str) or not account_id:
                                raise SupabaseConfigError("Plaid transaction is missing account_id")
                            change_type = "removed" if payload.get("removed") is True else "added"
                            cursor.execute(
                                f"""
                                insert into {raw_bank_table}
                                    (organization_id, run_id, source_batch_id, item_id, account_id,
                                     provider_record_id, change_type, payload_json, content_hash, observed_at)
                                values (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s::jsonb, %s, %s)
                                on conflict do nothing
                                """,
                                (
                                    organization_id,
                                    run_id,
                                    batch.batch_id,
                                    identity,
                                    account_id,
                                    record.provider_record_id,
                                    change_type,
                                    record.payload_json,
                                    record.content_hash,
                                    record.observed_at,
                                ),
                            )
                cursor.execute(
                    """
                    insert into normalized.source_snapshots
                        (id, organization_id, run_id, deployment_id, deployment_mode,
                         data_class, snapshot_cutoff_at, source_batch_ids, status)
                    values (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s::jsonb, 'complete')
                    """,
                    (
                        snapshot.snapshot_id,
                        organization_id,
                        run_id,
                        snapshot.deployment_id,
                        snapshot.mode,
                        snapshot.data_class,
                        snapshot.cutoff_at,
                        dumps(list(snapshot.source_batch_ids)),
                    ),
                )
                for record in snapshot.records:
                    cursor.execute(
                        """
                        insert into normalized.snapshot_records
                            (snapshot_id, normalized_record_version_id, source_batch_id,
                             provider, provider_record_id, content_hash)
                        values (%s::uuid, %s, %s::uuid, %s, %s, %s)
                        """,
                        (
                            snapshot.snapshot_id,
                            record.record_version_id,
                            record.source_batch_id,
                            record.provider,
                            record.provider_record_id,
                            record.content_hash,
                        ),
                    )
                cursor.execute(
                    """
                    update workflow.close_runs
                    set snapshot_id = %s::uuid, updated_at = now()
                    where id = %s::uuid
                    returning id::text, organization_id, period_start::text, period_end::text,
                              state, deployment_mode, data_class, snapshot_id::text, package_hash
                    """,
                    (snapshot.snapshot_id, run_id),
                )
                run_row = cursor.fetchone()
                self._record_task_event(
                    cursor,
                    organization_id=organization_id,
                    run_id=run_id,
                    task_id=None,
                    event_type="source_snapshot_committed",
                    payload={"snapshot_id": snapshot.snapshot_id, "record_count": len(snapshot.records)},
                )
            return self._run_from_row(run_row)
        finally:
            self._close(connection)

    def persist_evidence_batch(self, *, run_id: str, batch: EvidenceBatch) -> None:
        """Write scoped evidence metadata only; document/email bodies stay with providers."""
        batch.validate()
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    "select organization_id from workflow.close_runs where id = %s::uuid for update",
                    (run_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise SupabaseConfigError("close run does not exist")
                organization_id = str(row[0])
                for item in batch.items:
                    cursor.execute(
                        """
                        insert into normalized.evidence_items
                            (evidence_id, organization_id, run_id, provider, source_id,
                             content_hash, observed_at, kind, scope_reference, tags, metadata_json)
                        values (%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                        on conflict (evidence_id) do nothing
                        returning organization_id, run_id::text
                        """,
                        (
                            item.evidence_id,
                            organization_id,
                            run_id,
                            item.provider,
                            item.source_id,
                            item.content_hash,
                            item.observed_at,
                            item.kind,
                            item.scope_reference,
                            dumps(sorted(item.tags)),
                            dumps(dict(item.metadata)),
                        ),
                    )
                    persisted_context = cursor.fetchone()
                    if persisted_context is None:
                        cursor.execute(
                            """
                            select organization_id, run_id::text
                            from normalized.evidence_items
                            where evidence_id = %s
                            """,
                            (item.evidence_id,),
                        )
                        persisted_context = cursor.fetchone()
                    if persisted_context is None or (
                        str(persisted_context[0]), str(persisted_context[1])
                    ) != (organization_id, run_id):
                        raise SupabaseConfigError("evidence item identity is already bound to another close run")
                self._record_task_event(
                    cursor,
                    organization_id=organization_id,
                    run_id=run_id,
                    task_id=None,
                    event_type="evidence_batch_committed",
                    payload={"evidence_count": len(batch.items), "query_ids": list(batch.query_ids)},
                )
        finally:
            self._close(connection)

    def get_close_run(self, run_id: str) -> PersistedCloseRun | None:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select id::text, organization_id, period_start::text, period_end::text,
                           state, deployment_mode, data_class, snapshot_id::text, package_hash
                    from workflow.close_runs where id = %s::uuid
                    """,
                    (run_id,),
                )
                row = cursor.fetchone()
            return self._run_from_row(row) if row is not None else None
        finally:
            self._close(connection)

    def connections_for_organization(self, organization_id: str) -> tuple[PersistedConnection, ...]:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    select id::text, organization_id, provider, provider_environment,
                           provider_tenant_or_account_id, status, granted_scopes,
                           last_verified_at, last_success_at, consent_expires_at,
                           metadata_json ->> 'remediation'
                    from workflow.connections
                    where organization_id = %s
                    order by provider, provider_tenant_or_account_id
                    """,
                    (organization_id,),
                )
                rows = cursor.fetchall()
            return tuple(
                PersistedConnection(
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                    str(row[5]),
                    tuple(str(item) for item in (row[6] or [])),
                    row[7],
                    row[8],
                    row[9],
                    str(row[10]) if row[10] is not None else None,
                )
                for row in rows
            )
        finally:
            self._close(connection)

    @staticmethod
    def _ensure_default_tasks(cursor: Cursor, run_id: str, organization_id: str) -> None:
        task_ids: dict[str, str] = {}
        created_any = False
        for task_key, dependencies in DEFAULT_CLOSE_TASKS:
            cursor.execute(
                """
                insert into workflow.tasks (run_id, task_key, state, idempotency_key)
                values (%s::uuid, %s, %s, %s)
                on conflict (run_id, task_key) do nothing
                returning id::text
                """,
                (
                    run_id,
                    task_key,
                    "ready" if not dependencies else "pending",
                    f"{run_id}:{task_key}",
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                task_ids[task_key] = str(inserted[0])
                created_any = True
        for task_key, _ in DEFAULT_CLOSE_TASKS:
            if task_key not in task_ids:
                cursor.execute(
                    "select id::text from workflow.tasks where run_id = %s::uuid and task_key = %s",
                    (run_id, task_key),
                )
                task_ids[task_key] = str(cursor.fetchone()[0])
        for task_key, dependencies in DEFAULT_CLOSE_TASKS:
            for dependency in dependencies:
                cursor.execute(
                    """
                    insert into workflow.task_dependencies (task_id, depends_on_task_id)
                    values (%s::uuid, %s::uuid)
                    on conflict do nothing
                    """,
                    (task_ids[task_key], task_ids[dependency]),
                )
        if created_any:
            cursor.execute(
                "select id::text from workflow.tasks where run_id = %s::uuid and task_key = 'preflight'",
                (run_id,),
            )
            task_row = cursor.fetchone()
            SupabaseWorkflowStore._record_task_event(
                cursor,
                organization_id=organization_id,
                run_id=run_id,
                task_id=str(task_row[0]) if task_row is not None else None,
                event_type="run_created",
                payload={"task_count": len(DEFAULT_CLOSE_TASKS)},
            )

    def _finish_task(
        self,
        task: PersistedTask,
        worker_id: str,
        *,
        state: str,
        error: str | None,
    ) -> None:
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    update workflow.tasks
                    set state = %s, lease_owner = null, lease_expires_at = null,
                        last_error = %s, updated_at = now()
                    where id = %s::uuid and state = 'running' and lease_owner = %s
                    returning run_id::text, task_key
                    """,
                    (state, error, task.task_id, worker_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise SupabaseConfigError("task is not owned by this worker")
                run_id, task_key = str(row[0]), str(row[1])
                cursor.execute("select organization_id from workflow.close_runs where id = %s::uuid", (run_id,))
                organization_row = cursor.fetchone()
                if organization_row is None:
                    raise SupabaseConfigError("task close run does not exist")
                organization_id = str(organization_row[0])
                self._record_task_event(
                    cursor,
                    organization_id=organization_id,
                    run_id=run_id,
                    task_id=task.task_id,
                    event_type="task_completed" if state == "succeeded" else "task_blocked",
                    payload={"task_key": task_key, "error": error} if error else {"task_key": task_key},
                )
                if state == "blocked":
                    cursor.execute(
                        """
                        update workflow.close_runs set state = 'blocked', updated_at = now()
                        where id = %s::uuid and state <> 'cancelled'
                        """,
                        (run_id,),
                    )
                    return
                cursor.execute(
                    """
                    update workflow.tasks candidate
                    set state = 'ready', updated_at = now()
                    where candidate.run_id = %s::uuid and candidate.state = 'pending'
                      and exists (
                        select 1 from workflow.close_runs run
                        where run.id = candidate.run_id and run.state <> 'cancelled'
                      )
                      and not exists (
                        select 1
                        from workflow.task_dependencies dependency
                        join workflow.tasks prerequisite on prerequisite.id = dependency.depends_on_task_id
                        where dependency.task_id = candidate.id and prerequisite.state <> 'succeeded'
                      )
                    """,
                    (run_id,),
                )
                cursor.execute(
                    "select count(*) from workflow.tasks where run_id = %s::uuid and state <> 'succeeded'",
                    (run_id,),
                )
                incomplete = int(cursor.fetchone()[0])
                if incomplete == 0:
                    cursor.execute(
                        """
                        update workflow.close_runs set state = 'running', updated_at = now()
                        where id = %s::uuid and state = 'synchronizing'
                        """,
                        (run_id,),
                    )
                    self._record_task_event(
                        cursor,
                        organization_id=organization_id,
                        run_id=run_id,
                        task_id=None,
                        event_type="run_ready_for_review",
                        payload={},
                    )
        finally:
            self._close(connection)

    @staticmethod
    def _record_task_event(
        cursor: Cursor,
        *,
        organization_id: str,
        run_id: str,
        task_id: str | None,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        cursor.execute(
            """
            insert into workflow.task_events
                (organization_id, run_id, task_id, event_type, payload_json)
            values (%s, %s::uuid, %s::uuid, %s, %s::jsonb)
            """,
            (organization_id, run_id, task_id, event_type, dumps(payload, default=str)),
        )

    @staticmethod
    def _task_from_row(row: Sequence[object]) -> PersistedTask:
        return PersistedTask(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            int(row[4]),
            str(row[5]) if row[5] is not None else None,
            row[6],
            str(row[7]) if row[7] is not None else None,
            tuple(str(item) for item in (row[8] or ())) if len(row) > 8 else (),
        )

    @staticmethod
    def _event_from_row(row: Sequence[object]) -> PersistedTaskEvent:
        payload = row[5] if isinstance(row[5], Mapping) else {}
        return PersistedTaskEvent(
            int(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]) if row[3] is not None else None,
            str(row[4]),
            payload,
            row[6],
        )

    @staticmethod
    def _review_package_from_row(row: Sequence[object]) -> PersistedReviewPackage:
        summary = row[6] if isinstance(row[6], Mapping) else {}
        return PersistedReviewPackage(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            summary,
            row[7],
        )

    def upsert_connection(
        self,
        *,
        connection_health: ConnectionHealth,
        credential_secret_ref: str,
    ) -> PersistedConnection:
        """Persist a verified provider connection without storing token material.

        Provider tokens remain in the configured secret store.  The workflow
        database stores only the opaque secret reference and the observable
        connection-health metadata needed by the controller UI and workers.
        """
        if not credential_secret_ref.startswith("secret://"):
            raise SupabaseConfigError("connection credentials must be a secret-manager reference")
        connection = connect(self.config)
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    insert into workflow.connections
                        (organization_id, provider, provider_environment,
                         provider_tenant_or_account_id, credential_secret_ref,
                         status, granted_scopes, last_verified_at,
                         last_success_at, consent_expires_at, metadata_json)
                    values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
                    on conflict (organization_id, provider, provider_tenant_or_account_id)
                    do update set
                        provider_environment = excluded.provider_environment,
                        credential_secret_ref = excluded.credential_secret_ref,
                        status = excluded.status,
                        granted_scopes = excluded.granted_scopes,
                        last_verified_at = excluded.last_verified_at,
                        last_success_at = excluded.last_success_at,
                        consent_expires_at = excluded.consent_expires_at,
                        metadata_json = excluded.metadata_json
                    returning id::text, organization_id, provider, provider_environment,
                              provider_tenant_or_account_id, status, granted_scopes,
                              last_verified_at, last_success_at, consent_expires_at,
                              metadata_json ->> 'remediation'
                    """,
                    (
                        connection_health.organization_id,
                        connection_health.provider,
                        connection_health.provider_environment,
                        connection_health.provider_tenant_or_account_id,
                        credential_secret_ref,
                        connection_health.status.value
                        if isinstance(connection_health.status, ConnectionStatus)
                        else str(connection_health.status),
                        dumps(list(connection_health.granted_scopes)),
                        connection_health.last_verified_at,
                        connection_health.last_success_at,
                        connection_health.consent_expires_at,
                        dumps(
                            {"remediation": connection_health.remediation}
                            if connection_health.remediation
                            else {}
                        ),
                    ),
                )
                row = cursor.fetchone()
            return PersistedConnection(
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                tuple(str(item) for item in (row[6] or [])),
                row[7],
                row[8],
                row[9],
                str(row[10]) if row[10] is not None else None,
            )
        finally:
            self._close(connection)

    @staticmethod
    def _run_from_row(row: Sequence[object]) -> PersistedCloseRun:
        return PersistedCloseRun(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]) if row[7] is not None else None,
            str(row[8]) if row[8] is not None else None,
        )

    @staticmethod
    def _close(connection: Connection) -> None:
        close = getattr(connection, "close", None)
        if close is not None:
            close()


class PostgresOAuthSessionStore:
    """Durable OAuth transaction store backed by ``workflow.oauth_sessions``.

    Implements the ``OAuthSessionStore`` protocol (``put``/``consume``) so it is
    a drop-in replacement for the in-memory store. Unlike that store it survives
    a process restart and is shared across workers, so an authorize request on
    one process and its callback on another still find the same transaction.

    A fresh connection is opened per operation via the injected factory: the
    authorize and callback halves happen in different requests, and each must be
    its own committed transaction. ``consume`` deletes and returns the row in a
    single statement so a state value can be redeemed exactly once even under
    concurrent callbacks, and it filters expired rows so a stale state cannot be
    replayed.
    """

    def __init__(self, connect_factory: "Callable[[], Connection]") -> None:
        self._connect = connect_factory

    def put(self, oauth_transaction: OAuthTransaction, organization_id: str) -> None:
        if not organization_id:
            raise SupabaseConfigError("OAuth organization ID is required")
        connection = self._connect()
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    insert into workflow.oauth_sessions
                        (state, provider, organization_id, code_verifier,
                         code_challenge, redirect_uri, oidc, nonce, expires_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (state) do nothing
                    """,
                    (
                        oauth_transaction.state,
                        oauth_transaction.provider,
                        organization_id,
                        oauth_transaction.code_verifier,
                        oauth_transaction.code_challenge,
                        oauth_transaction.redirect_uri,
                        oauth_transaction.oidc,
                        oauth_transaction.nonce,
                        oauth_transaction.expires_at,
                    ),
                )
        finally:
            self._close(connection)

    def consume(self, state: str) -> tuple[OAuthTransaction, str] | None:
        if not state:
            return None
        connection = self._connect()
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    """
                    delete from workflow.oauth_sessions
                    where state = %s and expires_at > now()
                    returning provider, state, code_verifier, code_challenge,
                              redirect_uri, expires_at, oidc, nonce, organization_id
                    """,
                    (state,),
                )
                row = cursor.fetchone()
        finally:
            self._close(connection)
        if row is None:
            return None
        (
            provider,
            state_value,
            code_verifier,
            code_challenge,
            redirect_uri,
            expires_at,
            oidc,
            nonce,
            organization_id,
        ) = row
        return (
            OAuthTransaction(
                provider,
                state_value,
                code_verifier,
                code_challenge,
                redirect_uri,
                expires_at,
                bool(oidc),
                nonce,
            ),
            organization_id,
        )

    @staticmethod
    def _close(connection: Connection) -> None:
        close = getattr(connection, "close", None)
        if close is not None:
            close()


def oauth_session_store_from_environment(
    env: Mapping[str, str] | None = None,
) -> PostgresOAuthSessionStore:
    """Build a durable OAuth session store from ``SUPABASE_DB_URL``.

    Each call to the store opens a fresh, TLS-required Postgres connection via
    :func:`connect`, so the authorize and callback halves — which run in
    separate requests, possibly on separate workers — each get an independent
    committed transaction. Raises :class:`SupabaseConfigError` if the database
    is not configured, letting the caller fall back to the in-memory store.
    """
    config = SupabaseDatabaseConfig.from_environment(env)
    return PostgresOAuthSessionStore(lambda: connect(config))
