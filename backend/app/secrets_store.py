"""Concrete secret stores for the ``secret://`` reference contract.

The OAuth client and provider adapters never take raw credentials; they take
``secret://`` references and resolve them through a :class:`SecretStore`. This
module provides two implementations:

* :class:`EnvFileSecretStore` — the default for the isolated demo. Bootstrap
  secrets (client secret, seed refresh token) are read from environment
  variables; rotated refresh tokens are written durably to a JSON file outside
  the repo so a process restart does not brick the Xero connection.
* :class:`InMemorySecretStore` — deterministic store for tests.

A managed backend (Supabase Vault, cloud secret manager) can implement the same
``resolve``/``store`` contract and be swapped in without touching call sites.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


class SecretStoreError(RuntimeError):
    """Raised when a secret cannot be resolved or persisted."""


def _ref_to_env_key(secret_ref: str) -> str:
    """Map ``secret://xero/demo/client-secret`` -> ``SECRET_XERO_DEMO_CLIENT_SECRET``."""
    body = secret_ref[len("secret://") :]
    normalized = body.replace("/", "_").replace("-", "_").upper()
    return f"SECRET_{normalized}"


class InMemorySecretStore:
    """Non-persistent store for tests and local experiments."""

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(initial or {})

    def resolve(self, secret_ref: str) -> str:
        if not secret_ref.startswith("secret://"):
            raise SecretStoreError("secret references must start with secret://")
        value = self._values.get(secret_ref, "")
        if not value:
            raise SecretStoreError("secret reference is unavailable")
        return value

    def store(self, secret_ref: str, value: str) -> None:
        if not secret_ref.startswith("secret://"):
            raise SecretStoreError("secret references must start with secret://")
        if not value:
            raise SecretStoreError("refusing to store an empty secret")
        self._values[secret_ref] = value


class EnvFileSecretStore:
    """Env-seeded store that persists rotated secrets to a JSON file.

    Resolution order for a reference:

    1. A previously persisted (rotated) value in ``persistence_path``.
    2. The environment variable ``SECRET_<REF>`` (see :func:`_ref_to_env_key`).

    Writes go only to the persistence file, which must live outside any synced
    or version-controlled directory. Bootstrap env values are never mutated.
    """

    def __init__(
        self,
        persistence_path: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._path = Path(persistence_path)
        self._env = os.environ if env is None else env
        self._persisted: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SecretStoreError("persisted secret store is unreadable") from exc
        if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
            raise SecretStoreError("persisted secret store is malformed")
        return {str(k): v for k, v in data.items()}

    def resolve(self, secret_ref: str) -> str:
        if not secret_ref.startswith("secret://"):
            raise SecretStoreError("secret references must start with secret://")
        if secret_ref in self._persisted:
            return self._persisted[secret_ref]
        value = self._env.get(_ref_to_env_key(secret_ref), "")
        if not value or value.startswith("replace-"):
            raise SecretStoreError(f"secret reference {secret_ref} is unavailable")
        return value

    def store(self, secret_ref: str, value: str) -> None:
        if not secret_ref.startswith("secret://"):
            raise SecretStoreError("secret references must start with secret://")
        if not value:
            raise SecretStoreError("refusing to store an empty secret")
        updated = dict(self._persisted)
        updated[secret_ref] = value
        self._atomic_write(updated)
        self._persisted = updated

    def _atomic_write(self, data: Mapping[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(data, sort_keys=True, indent=2)
        try:
            handle = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self._path.parent),
                prefix=self._path.name,
                suffix=".tmp",
                delete=False,
            )
            with handle as tmp:
                tmp.write(serialized)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(handle.name, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass  # Best-effort on filesystems without POSIX permissions.
        except OSError as exc:
            raise SecretStoreError("could not persist rotated secret") from exc


def secret_store_from_environment(env: Mapping[str, str] | None = None) -> EnvFileSecretStore:
    """Build the default demo secret store.

    ``ACCOUNTINGOS_SECRET_STORE_PATH`` selects the persistence file; it defaults
    to ``.secrets/store.json`` under the current working directory. Point it at a
    path outside any synced/versioned folder in real deployments.
    """
    values = os.environ if env is None else env
    path = values.get("ACCOUNTINGOS_SECRET_STORE_PATH", "").strip() or ".secrets/store.json"
    return EnvFileSecretStore(path, values)
