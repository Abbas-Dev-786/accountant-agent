"""Read-only production readiness checks for AccountingOS.

The command deliberately validates configuration, Supabase schema state, and
Vault-backed static credentials without printing a secret or contacting a
provider API. Provider OAuth consent and close execution remain controller
actions in the browser/worker, respectively.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

from .b2 import B2Config, B2Error
from .google_oauth import GoogleOAuthConfig, GoogleOAuthError
from .plaid_link import PlaidLinkConfig, PlaidLinkError
from .secrets_store import SecretStoreError
from .supabase_auth import AuthenticationUnavailable, SupabaseAuthConfig
from .supabase_db import SupabaseConfigError, SupabaseDatabaseConfig, connect, transaction
from .xero_oauth import XeroOAuthConfig, XeroOAuthError


STATIC_SECRET_REFS: tuple[tuple[str, str], ...] = (
    ("Xero client secret", "ACCOUNTINGOS_XERO_CLIENT_SECRET_REF"),
    ("Plaid client secret", "PLAID_SECRET_REF"),
    ("Google client secret", "GOOGLE_CLIENT_SECRET_REF"),
    ("Groq API key", "GROQ_API_KEY_REF"),
    ("B2 key ID", "B2_KEY_ID_REF"),
    ("B2 application key", "B2_APPLICATION_KEY_REF"),
)


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or "replace-with" in normalized or normalized in {"todo", "tbd", "changeme"}


def _valid_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _check(name: str, action: Callable[[], None]) -> PreflightCheck:
    try:
        action()
    except Exception as exc:
        return PreflightCheck(name, False, str(exc) or "check failed")
    return PreflightCheck(name, True, "ok")


def _require_production_boundary(env: Mapping[str, str]) -> None:
    expected = {
        "ACCOUNTINGOS_DEPLOYMENT_MODE": "production",
        "ACCOUNTINGOS_DATA_CLASS": "live",
        "ACCOUNTINGOS_MARKET": "US",
        "ACCOUNTINGOS_CURRENCY": "USD",
    }
    mismatched = [key for key, value in expected.items() if env.get(key, "").strip() != value]
    if mismatched:
        raise ValueError(f"production boundary is invalid: {', '.join(mismatched)}")


def _require_web_configuration(env: Mapping[str, str]) -> None:
    required = ("ACCOUNTINGOS_CORS_ORIGINS", "ACCOUNTINGOS_WEB_APP_URL")
    invalid = [
        key
        for key in required
        if not _valid_https_url(env.get(key, "").strip()) or _is_placeholder(env.get(key, ""))
    ]
    if invalid:
        raise ValueError(f"production HTTPS URL is missing or invalid: {', '.join(invalid)}")


def _require_no_legacy_secret_configuration(env: Mapping[str, str]) -> None:
    legacy = []
    if env.get("ACCOUNTINGOS_SECRET_STORE_PATH", "").strip():
        legacy.append("ACCOUNTINGOS_SECRET_STORE_PATH")
    legacy.extend(sorted(key for key in env if key.startswith("SECRET_")))
    if legacy:
        raise ValueError(f"remove obsolete file-store/raw-secret variables: {', '.join(legacy)}")


def _validate_application_configs(env: Mapping[str, str]) -> None:
    validators: tuple[tuple[str, Callable[[], object]], ...] = (
        ("Supabase Auth", lambda: SupabaseAuthConfig.from_environment(env)),
        ("Supabase database", lambda: SupabaseDatabaseConfig.from_environment(env)),
        ("Xero OAuth", lambda: XeroOAuthConfig.from_environment(env)),
        ("Plaid Link", lambda: PlaidLinkConfig.from_environment(env)),
        ("Google OAuth", lambda: GoogleOAuthConfig.from_environment(env)),
        ("B2", lambda: B2Config.from_environment(env)),
    )
    errors: list[str] = []
    for name, validator in validators:
        try:
            validator()
        except (SupabaseConfigError, AuthenticationUnavailable, XeroOAuthError, PlaidLinkError, GoogleOAuthError, B2Error, ValueError) as exc:
            errors.append(f"{name}: {str(exc) or 'invalid configuration'}")

    required_https = (
        "SUPABASE_URL",
        "ACCOUNTINGOS_XERO_REDIRECT_URI",
        "PLAID_WEBHOOK_URL",
        "GOOGLE_REDIRECT_URI",
    )
    invalid_https = [
        key
        for key in required_https
        if not _valid_https_url(env.get(key, "").strip()) or _is_placeholder(env.get(key, ""))
    ]
    if invalid_https:
        errors.append(f"production HTTPS URL is missing or invalid: {', '.join(invalid_https)}")

    groq_reference = env.get("GROQ_API_KEY_REF", "").strip()
    if not groq_reference.startswith("secret://") or _is_placeholder(groq_reference):
        errors.append("GROQ_API_KEY_REF must be a Supabase Vault reference")
    if any(key.startswith("NEXT_PUBLIC_") and "GROQ" in key for key in env):
        errors.append("Groq credentials cannot be public client variables")
    tenant_allowlist = [
        value for value in env.get("ACCOUNTINGOS_XERO_TENANT_ALLOWLIST", "").replace(",", " ").split()
        if value and not _is_placeholder(value)
    ]
    if not tenant_allowlist:
        errors.append("ACCOUNTINGOS_XERO_TENANT_ALLOWLIST must name at least one approved Xero tenant")
    if errors:
        raise ValueError("; ".join(errors))


def _first_value(row: object) -> object | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    if isinstance(row, (tuple, list)):
        return row[0] if row else None
    return row


def _database_and_vault_checks(
    env: Mapping[str, str],
    connection_factory: Callable[[SupabaseDatabaseConfig], object] = connect,
) -> tuple[PreflightCheck, ...]:
    config = SupabaseDatabaseConfig.from_environment(env)
    checks: list[PreflightCheck] = []
    connection = connection_factory(config)
    try:
        with transaction(connection) as cursor:
            cursor.execute("set local transaction read only")
            cursor.execute(
                """
                select
                    exists (select 1 from pg_extension where extname = 'supabase_vault'),
                    to_regclass('workflow.close_runs') is not null,
                    to_regclass('workflow.task_events') is not null,
                    to_regclass('workflow.close_mappings') is not null,
                    to_regclass('workflow.reconciliations') is not null,
                    to_regclass('vault.secrets') is not null,
                    not has_schema_privilege('anon', 'vault', 'USAGE'),
                    not has_schema_privilege('authenticated', 'vault', 'USAGE'),
                    not has_table_privilege('anon', 'vault.decrypted_secrets', 'SELECT'),
                    not has_table_privilege('authenticated', 'vault.decrypted_secrets', 'SELECT')
                """
            )
            row = cursor.fetchone()
            values = tuple(row) if isinstance(row, (tuple, list)) else ()
            expected = (
                "Vault extension",
                "workflow close-runs schema",
                "workflow event schema",
                "versioned close-mapping schema",
                "durable reconciliation schema",
                "Vault secrets relation",
                "anon Vault schema access revoked",
                "authenticated Vault schema access revoked",
                "anon decrypted Vault access revoked",
                "authenticated decrypted Vault access revoked",
            )
            if len(values) != len(expected):
                raise RuntimeError("database readiness query returned an invalid result")
            checks.extend(
                PreflightCheck(name, bool(value), "ok" if value else "missing or unsafe")
                for name, value in zip(expected, values, strict=True)
            )

            for label, key in STATIC_SECRET_REFS:
                reference = env.get(key, "").strip()
                if _is_placeholder(reference) or not reference.startswith("secret://"):
                    checks.append(PreflightCheck(f"Vault {label}", False, f"{key} is invalid"))
                    continue
                cursor.execute(
                    "select decrypted_secret from vault.decrypted_secrets where name = %s",
                    (reference,),
                )
                value = _first_value(cursor.fetchone())
                valid = isinstance(value, str) and not _is_placeholder(value)
                checks.append(PreflightCheck(f"Vault {label}", valid, "ok" if valid else "missing or placeholder"))
    except Exception as exc:
        return (PreflightCheck("Supabase database and Vault", False, str(exc) or "connection failed"),)
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    return tuple(checks)


def production_preflight(
    env: Mapping[str, str] | None = None,
    *,
    connection_factory: Callable[[SupabaseDatabaseConfig], object] = connect,
) -> PreflightReport:
    """Run read-only checks and return no credential material."""
    values = os.environ if env is None else env
    checks = [
        _check("production deployment boundary", lambda: _require_production_boundary(values)),
        _check("production web/controller configuration", lambda: _require_web_configuration(values)),
        _check("obsolete secret configuration", lambda: _require_no_legacy_secret_configuration(values)),
        _check("server-side application configuration", lambda: _validate_application_configs(values)),
    ]
    if all(check.passed for check in checks):
        try:
            checks.extend(_database_and_vault_checks(values, connection_factory))
        except (SupabaseConfigError, AuthenticationUnavailable, XeroOAuthError, PlaidLinkError, GoogleOAuthError, B2Error, SecretStoreError) as exc:
            checks.append(PreflightCheck("Supabase database and Vault", False, str(exc)))
    return PreflightReport(tuple(checks))


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse assignment-only env files without executing their contents."""
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number} is not an environment assignment")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or not key[0].isalpha() or key != key.upper():
            raise ValueError(f"{path}:{number} has an invalid environment key")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only AccountingOS production readiness checks")
    parser.add_argument("--env-file", type=Path, help="optional assignment-only environment file; never source it")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.env_file:
        env.update(_parse_env_file(args.env_file))
    report = production_preflight(env)
    for check in report.checks:
        prefix = "PASS" if check.passed else "FAIL"
        print(f"{prefix}: {check.name} — {check.detail}")
    return 0 if report.ready else 1


if __name__ == "__main__":  # pragma: no cover - exercised as an operator command
    raise SystemExit(main())
