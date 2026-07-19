"""Supabase Auth verification for AccountingOS API requests.

The browser sends a Supabase access token as a Bearer token.  Rather than
trusting decoded browser claims, the API verifies that token with Supabase
Auth's ``/auth/v1/user`` endpoint.  This supports both modern asymmetric JWT
keys and legacy HS256 projects without ever copying a signing secret into the
application.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .domain import PolicyError


class AuthenticationError(PolicyError):
    """The supplied request cannot be authenticated by Supabase Auth."""


class AuthenticationUnavailable(AuthenticationError):
    """Supabase Auth configuration or the Auth service is unavailable."""


@dataclass(frozen=True)
class SupabaseAuthConfig:
    project_url: str
    publishable_key: str

    def __post_init__(self) -> None:
        parsed = urlparse(self.project_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise AuthenticationUnavailable("SUPABASE_URL must be an absolute HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise AuthenticationUnavailable("SUPABASE_URL must use HTTPS outside local development")
        if not self.publishable_key or self.publishable_key.startswith("replace-with"):
            raise AuthenticationUnavailable("SUPABASE_PUBLISHABLE_KEY must be configured server-side")

    @property
    def issuer(self) -> str:
        return f"{self.project_url.rstrip('/')}/auth/v1"

    @property
    def user_endpoint(self) -> str:
        return f"{self.issuer}/user"

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "SupabaseAuthConfig":
        values = os.environ if env is None else env
        if values.get("ACCOUNTINGOS_AUTH_PROVIDER", "supabase") != "supabase":
            raise AuthenticationUnavailable("ACCOUNTINGOS_AUTH_PROVIDER must be supabase")
        if any(key.startswith("NEXT_PUBLIC_") and "SERVICE_ROLE" in key for key in values):
            raise AuthenticationUnavailable("Supabase service-role credentials cannot be public")
        return cls(
            values.get("SUPABASE_URL", "").strip(),
            values.get("SUPABASE_PUBLISHABLE_KEY", "").strip(),
        )


@dataclass(frozen=True)
class SupabaseUser:
    subject: str
    email: str | None
    issuer: str


@dataclass(frozen=True)
class AuthResponse:
    status_code: int
    body: Mapping[str, object]


class AuthTransport(Protocol):
    def get(self, url: str, headers: Mapping[str, str]) -> AuthResponse:
        ...


class UrllibAuthTransport:
    def get(self, url: str, headers: Mapping[str, str]) -> AuthResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
                return AuthResponse(response.status, body if isinstance(body, Mapping) else {})
        except HTTPError as exc:
            return AuthResponse(exc.code, {})
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AuthenticationUnavailable("Supabase Auth verification is unavailable") from exc


class AuthVerifier(Protocol):
    def authenticate(self, token: str) -> SupabaseUser:
        ...


class SupabaseAuthVerifier:
    """Verify a Supabase-issued user token with the configured Auth service."""

    def __init__(self, config: SupabaseAuthConfig, transport: AuthTransport | None = None) -> None:
        self.config = config
        self.transport = transport or UrllibAuthTransport()

    def authenticate(self, token: str) -> SupabaseUser:
        if not token or len(token) > 16_384:
            raise AuthenticationError("Bearer token is invalid")
        response = self.transport.get(
            self.config.user_endpoint,
            {
                "apikey": self.config.publishable_key,
                "Authorization": f"Bearer {token}",
            },
        )
        if response.status_code in {401, 403}:
            raise AuthenticationError("Supabase access token is invalid or expired")
        if response.status_code >= 400:
            raise AuthenticationUnavailable("Supabase Auth verification failed")
        subject = response.body.get("id")
        email = response.body.get("email")
        if not isinstance(subject, str) or not subject:
            raise AuthenticationError("Supabase Auth response did not contain a user id")
        return SupabaseUser(subject, email if isinstance(email, str) else None, self.config.issuer)
