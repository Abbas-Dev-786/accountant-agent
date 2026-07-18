"""OIDC identity and OAuth callback safety primitives for Phase 1."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .domain import PolicyError


class SecurityError(PolicyError):
    """Raised when identity or OAuth callback validation fails."""


def _urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class OAuthTransaction:
    provider: str
    state: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str
    expires_at: datetime
    oidc: bool = False
    nonce: str | None = None


def create_oauth_transaction(
    provider: str, redirect_uri: str, *, now: datetime | None = None, ttl_seconds: int = 600, oidc: bool = False
) -> OAuthTransaction:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise SecurityError("OAuth transaction time must include a timezone")
    verifier = _urlsafe(secrets.token_bytes(32))
    challenge = _urlsafe(hashlib.sha256(verifier.encode("ascii")).digest())
    return OAuthTransaction(
        provider,
        _urlsafe(secrets.token_bytes(32)),
        verifier,
        challenge,
        redirect_uri,
        datetime.fromtimestamp(current.timestamp() + ttl_seconds, timezone.utc),
        oidc,
        _urlsafe(secrets.token_bytes(32)) if oidc else None,
    )


def validate_oauth_callback(
    transaction: OAuthTransaction,
    returned_state: str,
    returned_redirect_uri: str,
    *,
    now: datetime | None = None,
    returned_nonce: str | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    if current >= transaction.expires_at:
        raise SecurityError("OAuth transaction expired")
    if not hmac.compare_digest(transaction.state, returned_state):
        raise SecurityError("OAuth state mismatch")
    if transaction.redirect_uri != returned_redirect_uri:
        raise SecurityError("OAuth redirect URI mismatch")
    if transaction.oidc and (
        not transaction.nonce or not returned_nonce or not hmac.compare_digest(transaction.nonce, returned_nonce)
    ):
        raise SecurityError("OIDC nonce mismatch")


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    audience: str


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    audience: str


def validate_oidc_claims(claims: dict[str, Any], config: OIDCConfig, *, now: datetime | None = None) -> Identity:
    current = (now or datetime.now(timezone.utc)).timestamp()
    try:
        issuer = claims["iss"]
        subject = claims["sub"]
        audience = claims["aud"]
        expiry = float(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SecurityError("OIDC claims are incomplete") from exc
    audiences = audience if isinstance(audience, list) else [audience]
    if issuer != config.issuer or config.audience not in audiences:
        raise SecurityError("OIDC issuer or audience mismatch")
    if not subject or expiry <= current:
        raise SecurityError("OIDC subject is missing or token is expired")
    return Identity(issuer, subject, config.audience)
