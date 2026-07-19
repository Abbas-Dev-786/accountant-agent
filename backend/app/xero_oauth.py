"""Server-only Xero OAuth Auth Code + PKCE and token-refresh client.

This module deliberately handles tokens at the backend boundary. Access tokens
are returned to the caller for an immediate provider request; refresh tokens
are written only through the injected secret store and are never included in
URLs, database rows, or exception messages.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .security import OAuthTransaction


class XeroOAuthError(RuntimeError):
    """Raised when Xero OAuth configuration or token exchange fails."""


class SecretStore(Protocol):
    def resolve(self, secret_ref: str) -> str:
        ...

    def store(self, secret_ref: str, value: str) -> None:
        ...


class OAuthSessionStore(Protocol):
    def put(self, transaction: OAuthTransaction, organization_id: str) -> None:
        ...

    def consume(self, state: str) -> tuple[OAuthTransaction, str] | None:
        ...


class InMemoryOAuthSessionStore:
    """Single-process development store; production must persist sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[OAuthTransaction, str]] = {}

    def put(self, transaction: OAuthTransaction, organization_id: str) -> None:
        if not organization_id:
            raise XeroOAuthError("OAuth organization ID is required")
        self._sessions[transaction.state] = (transaction, organization_id)

    def consume(self, state: str) -> tuple[OAuthTransaction, str] | None:
        return self._sessions.pop(state, None)


class FormTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], form: Mapping[str, str]) -> "FormResponse":
        ...


@dataclass(frozen=True)
class FormResponse:
    status_code: int
    body: Mapping[str, object]
    headers: Mapping[str, str]


class UrllibFormTransport:
    def post(self, url: str, headers: Mapping[str, str], form: Mapping[str, str]) -> FormResponse:
        request = Request(
            url,
            data=urlencode(form).encode("utf-8"),
            headers={**headers, "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
                if not isinstance(body, Mapping):
                    raise XeroOAuthError("Xero token response is not an object")
                return FormResponse(response.status, body, dict(response.headers.items()))
        except HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                body = {}
            return FormResponse(exc.code, body if isinstance(body, Mapping) else {}, dict(exc.headers.items()))
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise XeroOAuthError("Xero token request failed") from exc


@dataclass(frozen=True)
class XeroOAuthConfig:
    client_id: str
    client_secret_ref: str
    refresh_token_secret_ref: str
    redirect_uri: str
    scopes: tuple[str, ...]
    token_endpoint: str = "https://identity.xero.com/connect/token"
    authorize_endpoint: str = "https://login.xero.com/identity/connect/authorize"

    def __post_init__(self) -> None:
        if not self.client_id or self.client_id.startswith("replace-"):
            raise XeroOAuthError("Xero client ID must be configured")
        if not self.client_secret_ref.startswith("secret://"):
            raise XeroOAuthError("Xero client secret must be a secret:// reference")
        if not self.refresh_token_secret_ref.startswith("secret://"):
            raise XeroOAuthError("Xero refresh token must be a secret:// reference")
        redirect = urlsplit(self.redirect_uri)
        if redirect.scheme not in {"http", "https"} or not redirect.netloc:
            raise XeroOAuthError("Xero redirect URI must be an absolute HTTP(S) URL")
        if redirect.scheme == "http" and redirect.hostname != "localhost":
            raise XeroOAuthError("HTTP redirect URIs are allowed only for localhost")
        if redirect.hostname == "127.0.0.1":
            raise XeroOAuthError("Xero redirect URI must use localhost, not 127.0.0.1")
        if not self.token_endpoint.startswith("https://") or not self.authorize_endpoint.startswith("https://"):
            raise XeroOAuthError("Xero OAuth endpoints must use HTTPS")
        if not self.scopes or "offline_access" not in self.scopes:
            raise XeroOAuthError("Xero OAuth scopes must include offline_access")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "XeroOAuthConfig":
        values = os.environ if env is None else env
        scopes = tuple(values.get("ACCOUNTINGOS_XERO_SCOPES", "").split())
        return cls(
            values.get("ACCOUNTINGOS_XERO_CLIENT_ID", ""),
            values.get("ACCOUNTINGOS_XERO_CLIENT_SECRET_REF", ""),
            values.get("ACCOUNTINGOS_XERO_REFRESH_TOKEN_SECRET_REF", ""),
            values.get("ACCOUNTINGOS_XERO_REDIRECT_URI", ""),
            scopes,
            values.get("ACCOUNTINGOS_XERO_TOKEN_ENDPOINT", "https://identity.xero.com/connect/token"),
            values.get("ACCOUNTINGOS_XERO_AUTHORIZE_ENDPOINT", "https://login.xero.com/identity/connect/authorize"),
        )


@dataclass(frozen=True)
class XeroToken:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
    scope: str | None = None


class XeroOAuthClient:
    def __init__(self, config: XeroOAuthConfig, secrets: SecretStore, transport: FormTransport | None = None) -> None:
        self.config = config
        self.secrets = secrets
        self.transport = transport or UrllibFormTransport()
        self._cached_token: XeroToken | None = None
        self._cached_until: datetime | None = None

    def authorization_url(self, state: str, code_challenge: str) -> str:
        if not state or not code_challenge:
            raise XeroOAuthError("OAuth state and PKCE challenge are required")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        parts = urlsplit(self.config.authorize_endpoint)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))

    def exchange_code(self, code: str, code_verifier: str) -> XeroToken:
        if not code or not code_verifier:
            raise XeroOAuthError("authorization code and PKCE verifier are required")
        token = self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": code_verifier,
            }
        )
        self._store_refresh_token(token.refresh_token)
        self._cache(token)
        return token

    def refresh(self) -> XeroToken:
        refresh_token = self.secrets.resolve(self.config.refresh_token_secret_ref)
        if not refresh_token or refresh_token.startswith("replace-"):
            raise XeroOAuthError("Xero refresh token is unavailable")
        token = self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        self._store_refresh_token(token.refresh_token)
        self._cache(token)
        return token

    def access_token(self, *, now: datetime | None = None, refresh_skew_seconds: int = 60) -> str:
        """Return a cached access token or refresh it before expiry."""
        if refresh_skew_seconds < 0:
            raise XeroOAuthError("refresh skew must not be negative")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise XeroOAuthError("token time must include a timezone")
        if self._cached_token and self._cached_until and current + timedelta(seconds=refresh_skew_seconds) < self._cached_until:
            return self._cached_token.access_token
        return self.refresh().access_token

    def _cache(self, token: XeroToken, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        self._cached_token = token
        self._cached_until = current + timedelta(seconds=token.expires_in)

    def _store_refresh_token(self, refresh_token: str) -> None:
        try:
            self.secrets.store(self.config.refresh_token_secret_ref, refresh_token)
        except Exception as exc:
            raise XeroOAuthError("Xero refresh token could not be persisted") from exc

    def _token(self, form: Mapping[str, str]) -> XeroToken:
        client_secret = self.secrets.resolve(self.config.client_secret_ref)
        if not client_secret or client_secret.startswith("replace-"):
            raise XeroOAuthError("Xero client secret is unavailable")
        basic = base64.b64encode(f"{self.config.client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        response = self.transport.post(
            self.config.token_endpoint,
            {"Authorization": f"Basic {basic}"},
            form,
        )
        if response.status_code >= 400:
            raise XeroOAuthError(f"Xero token endpoint returned HTTP {response.status_code}")
        access_token = response.body.get("access_token")
        refresh_token = response.body.get("refresh_token")
        expires_in = response.body.get("expires_in")
        token_type = response.body.get("token_type", "Bearer")
        if (
            not isinstance(access_token, str)
            or not isinstance(refresh_token, str)
            or not isinstance(expires_in, int)
            or expires_in <= 0
            or not isinstance(token_type, str)
        ):
            raise XeroOAuthError("Xero token response is incomplete")
        scope = response.body.get("scope")
        return XeroToken(access_token, refresh_token, expires_in, token_type, scope if isinstance(scope, str) else None)
