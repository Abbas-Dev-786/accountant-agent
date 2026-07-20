"""Server-only Google Workspace OAuth with PKCE and durable secret references."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .security import OAuthTransaction
from .xero_oauth import SecretStore


class GoogleOAuthError(RuntimeError):
    """Raised when Google authorization cannot complete safely."""


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret_ref: str
    redirect_uri: str
    scopes: tuple[str, ...]
    token_endpoint: str = "https://oauth2.googleapis.com/token"
    authorize_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth"

    def __post_init__(self) -> None:
        if not self.client_id or self.client_id.startswith("replace-"):
            raise GoogleOAuthError("Google client ID must be configured")
        if not self.client_secret_ref.startswith("secret://"):
            raise GoogleOAuthError("Google client secret must be a secret:// reference")
        redirect = urlsplit(self.redirect_uri)
        if redirect.scheme not in {"http", "https"} or not redirect.netloc:
            raise GoogleOAuthError("Google redirect URI must be an absolute HTTP(S) URL")
        if redirect.scheme == "http" and redirect.hostname not in {"localhost", "127.0.0.1"}:
            raise GoogleOAuthError("HTTP redirect URIs are allowed only for local development")
        if not self.scopes:
            raise GoogleOAuthError("Google OAuth scopes must be configured")
        if not self.token_endpoint.startswith("https://") or not self.authorize_endpoint.startswith("https://"):
            raise GoogleOAuthError("Google OAuth endpoints must use HTTPS")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "GoogleOAuthConfig":
        values = os.environ if env is None else env
        scopes = tuple(
            values.get(
                "ACCOUNTINGOS_GOOGLE_SCOPES",
                "https://www.googleapis.com/auth/drive.metadata.readonly "
                "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/gmail.compose "
                "https://www.googleapis.com/auth/gmail.send",
            ).split()
        )
        return cls(
            values.get("GOOGLE_CLIENT_ID", ""),
            values.get("GOOGLE_CLIENT_SECRET_REF", ""),
            values.get("GOOGLE_REDIRECT_URI", ""),
            scopes,
            values.get("GOOGLE_TOKEN_ENDPOINT", "https://oauth2.googleapis.com/token"),
            values.get("GOOGLE_AUTHORIZE_ENDPOINT", "https://accounts.google.com/o/oauth2/v2/auth"),
        )


@dataclass(frozen=True)
class GoogleToken:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str | None


class FormTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], form: Mapping[str, str]) -> "FormResponse":
        ...


@dataclass(frozen=True)
class FormResponse:
    status_code: int
    body: Mapping[str, object]


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
                raw = json.loads(response.read().decode("utf-8"))
                if not isinstance(raw, Mapping):
                    raise GoogleOAuthError("Google token response is not an object")
                return FormResponse(response.status, raw)
        except HTTPError as exc:
            try:
                raw = json.loads(exc.read().decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                raw = {}
            return FormResponse(exc.code, raw if isinstance(raw, Mapping) else {})
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GoogleOAuthError("Google token request failed") from exc


class GoogleOAuthClient:
    def __init__(self, config: GoogleOAuthConfig, secrets: SecretStore, transport: FormTransport | None = None) -> None:
        self.config = config
        self.secrets = secrets
        self.transport = transport or UrllibFormTransport()

    def authorization_url(self, transaction: OAuthTransaction) -> str:
        if transaction.provider != "drive":
            raise GoogleOAuthError("Google OAuth transaction must be a Drive transaction")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": transaction.state,
                "code_challenge": transaction.code_challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "false",
            }
        )
        parts = urlsplit(self.config.authorize_endpoint)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))

    def exchange_code(self, code: str, transaction: OAuthTransaction) -> GoogleToken:
        if not code or not transaction.code_verifier:
            raise GoogleOAuthError("Google authorization code and PKCE verifier are required")
        return self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.config.client_id,
                "client_secret": self.secrets.resolve(self.config.client_secret_ref),
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": transaction.code_verifier,
            },
            require_refresh=True,
        )

    def refresh_access_token(self, refresh_token_secret_ref: str, access_token_secret_ref: str) -> str:
        refresh_token = self.secrets.resolve(refresh_token_secret_ref)
        token = self._token(
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "client_secret": self.secrets.resolve(self.config.client_secret_ref),
                "refresh_token": refresh_token,
            },
            require_refresh=False,
        )
        self.secrets.store(access_token_secret_ref, token.access_token)
        return token.access_token

    def _token(self, form: Mapping[str, str], *, require_refresh: bool) -> GoogleToken:
        response = self.transport.post(self.config.token_endpoint, {}, form)
        if response.status_code >= 400:
            raise GoogleOAuthError(f"Google token endpoint returned HTTP {response.status_code}")
        access_token = response.body.get("access_token")
        refresh_token = response.body.get("refresh_token")
        expires_in = response.body.get("expires_in")
        if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, int) or expires_in <= 0:
            raise GoogleOAuthError("Google token response is incomplete")
        if require_refresh and (not isinstance(refresh_token, str) or not refresh_token):
            raise GoogleOAuthError("Google authorization did not return an offline refresh token")
        scope = response.body.get("scope")
        return GoogleToken(access_token, refresh_token if isinstance(refresh_token, str) else None, expires_in, scope if isinstance(scope, str) else None)
