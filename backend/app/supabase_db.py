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
from json import dumps
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import parse_qs, urlparse

from .domain import CloseRun, PolicyError, SourceBatch, SourceSnapshot
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
