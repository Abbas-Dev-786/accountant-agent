"""Secret stores for opaque ``secret://`` references.

Production credentials are encrypted in Supabase Vault. The workflow database
stores only the opaque reference; provider adapters receive the decrypted value
only in the server-side API or worker process. ``InMemorySecretStore`` remains
for deterministic unit tests.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Mapping

from .supabase_db import SupabaseConfigError, SupabaseDatabaseConfig, connect, transaction


class SecretStoreError(RuntimeError):
    """Raised when a secret cannot be resolved or persisted."""


def _validate_reference(secret_ref: str) -> None:
    if not secret_ref.startswith("secret://") or len(secret_ref) > 1024:
        raise SecretStoreError("secret references must be a bounded secret:// value")


class InMemorySecretStore:
    """Non-persistent store for tests and local experiments."""

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(initial or {})

    def resolve(self, secret_ref: str) -> str:
        _validate_reference(secret_ref)
        value = self._values.get(secret_ref, "")
        if not value:
            raise SecretStoreError("secret reference is unavailable")
        return value

    def store(self, secret_ref: str, value: str) -> None:
        _validate_reference(secret_ref)
        if not value:
            raise SecretStoreError("refusing to store an empty secret")
        self._values[secret_ref] = value

    def delete(self, secret_ref: str) -> None:
        _validate_reference(secret_ref)
        self._values.pop(secret_ref, None)


class SupabaseVaultSecretStore:
    """Store encrypted secrets in Supabase Vault using their opaque reference.

    A Vault secret's name is the same ``secret://`` reference kept in
    ``workflow.connections``. This keeps the database reference durable and
    useful without copying provider token material into workflow tables.
    """

    def __init__(
        self,
        config: SupabaseDatabaseConfig,
        connection_factory: Callable[[SupabaseDatabaseConfig], object] | None = None,
    ) -> None:
        self.config = config
        self._connection_factory = connection_factory or connect
        self._exclusive_lock_refs = threading.local()

    @staticmethod
    def _first_value(row: object) -> object | None:
        if row is None:
            return None
        if isinstance(row, Mapping):
            return next(iter(row.values()), None)
        if isinstance(row, (tuple, list)):
            return row[0] if row else None
        return row

    def resolve(self, secret_ref: str) -> str:
        _validate_reference(secret_ref)
        connection = self._open_connection()
        try:
            with transaction(connection) as cursor:
                cursor.execute(
                    "select decrypted_secret from vault.decrypted_secrets where name = %s",
                    (secret_ref,),
                )
                value = self._first_value(cursor.fetchone())
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError("Supabase Vault secret is unavailable") from exc
        finally:
            self._close_connection(connection)
        if not isinstance(value, str) or not value:
            raise SecretStoreError("Supabase Vault secret is unavailable")
        return value

    @contextmanager
    def exclusive_lock(self, secret_ref: str) -> Iterator[None]:
        """Hold a database-wide lock for a single-use provider credential.

        Vault reads and writes use separate short transactions, so the lock
        must be session-scoped and span the provider exchange between them.
        """
        _validate_reference(secret_ref)
        held_refs = getattr(self._exclusive_lock_refs, "refs", set())
        if secret_ref in held_refs:
            yield
            return
        connection = self._open_connection()
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (secret_ref,))
        except Exception as exc:
            self._close_connection(connection)
            raise SecretStoreError("provider credential lock is unavailable") from exc
        self._exclusive_lock_refs.refs = {*held_refs, secret_ref}
        try:
            yield
        finally:
            self._exclusive_lock_refs.refs = held_refs
            try:
                cursor.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (secret_ref,))
            except Exception:
                # Losing this session also releases PostgreSQL advisory locks.
                # Do not mask the provider result with a best-effort unlock.
                pass
            self._close_connection(connection)

    def store(self, secret_ref: str, value: str) -> None:
        _validate_reference(secret_ref)
        if not value:
            raise SecretStoreError("refusing to store an empty secret")
        connection = self._open_connection()
        try:
            with transaction(connection) as cursor:
                # Serialize competing OAuth refresh rotations for one opaque
                # reference. A collision can only cause harmless contention.
                if secret_ref not in getattr(self._exclusive_lock_refs, "refs", set()):
                    cursor.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (secret_ref,))
                cursor.execute("select id from vault.secrets where name = %s for update", (secret_ref,))
                secret_id = self._first_value(cursor.fetchone())
                if secret_id is None:
                    cursor.execute(
                        "select vault.create_secret(%s, %s, %s)",
                        (value, secret_ref, "AccountingOS server-managed provider credential"),
                    )
                else:
                    cursor.execute(
                        "select vault.update_secret(%s::uuid, %s, null, null)",
                        (str(secret_id), value),
                    )
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError("Supabase Vault secret could not be stored") from exc
        finally:
            self._close_connection(connection)

    def delete(self, secret_ref: str) -> None:
        """Remove a Vault credential once no healthy connection references it."""
        _validate_reference(secret_ref)
        connection = self._open_connection()
        try:
            with transaction(connection) as cursor:
                cursor.execute("select id from vault.secrets where name = %s for update", (secret_ref,))
                secret_id = self._first_value(cursor.fetchone())
                if secret_id is not None:
                    cursor.execute("select vault.delete_secret(%s::uuid)", (str(secret_id),))
        except Exception as exc:
            raise SecretStoreError("Supabase Vault secret could not be removed") from exc
        finally:
            self._close_connection(connection)

    def _open_connection(self):
        try:
            return self._connection_factory(self.config)
        except Exception as exc:
            raise SecretStoreError("Supabase Vault connection is unavailable") from exc

    @staticmethod
    def _close_connection(connection: object) -> None:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def secret_store_from_environment(env: Mapping[str, str] | None = None) -> SupabaseVaultSecretStore:
    """Build the production-only Vault-backed secret store."""
    values = os.environ if env is None else env
    try:
        config = SupabaseDatabaseConfig.from_environment(values)
    except SupabaseConfigError as exc:
        raise SecretStoreError("production secrets require SUPABASE_DB_URL and Supabase Vault") from exc
    return SupabaseVaultSecretStore(config)
