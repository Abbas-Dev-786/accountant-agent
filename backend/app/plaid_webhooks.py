"""Verified Plaid webhook handling using Plaid's signed JWT envelope."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import time
from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Event, Lock
from typing import Mapping

from .provider_runtime import JsonTransport, SecretResolver
from .providers import ProviderReadError


class PlaidWebhookError(ProviderReadError):
    """A Plaid webhook did not pass the provider's authenticity checks."""


def _base64url_json(value: str, label: str) -> Mapping[str, object]:
    try:
        padded = value + ("=" * (-len(value) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaidWebhookError(f"Plaid webhook {label} is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise PlaidWebhookError(f"Plaid webhook {label} is invalid")
    return decoded


def _base64url_bytes(value: str, label: str) -> bytes:
    try:
        padded = value + ("=" * (-len(value) % 4))
        return base64.urlsafe_b64decode(padded.encode())
    except (binascii.Error, ValueError) as exc:
        raise PlaidWebhookError(f"Plaid webhook {label} is invalid") from exc


@dataclass
class PlaidWebhookVerifier:
    """Verify the ``Plaid-Verification`` ES256 JWT against Plaid's JWK API."""

    client_id: str
    client_secret_ref: str
    secret_resolver: SecretResolver
    transport: JsonTransport
    base_url: str = "https://production.plaid.com"
    max_age_seconds: int = 300
    verification_key_ttl_seconds: int = 3600
    max_key_fetches_per_minute: int = 30
    _key_cache: dict[str, tuple[float, Mapping[str, object]]] = field(default_factory=dict, init=False, repr=False)
    _negative_key_cache: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _key_fetches: deque[float] = field(default_factory=deque, init=False, repr=False)
    _inflight_keys: dict[str, Event] = field(default_factory=dict, init=False, repr=False)
    _cache_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_age_seconds < 1 or self.verification_key_ttl_seconds < 1 or self.max_key_fetches_per_minute < 1:
            raise PlaidWebhookError("Plaid webhook verifier limits are invalid")

    def verify(self, signed_header: str, body: bytes) -> None:
        parts = signed_header.split(".")
        if len(parts) != 3:
            raise PlaidWebhookError("Plaid webhook signature is invalid")
        header = _base64url_json(parts[0], "header")
        key_id = header.get("kid")
        if header.get("alg") != "ES256" or not isinstance(key_id, str) or not 1 <= len(key_id) <= 256:
            raise PlaidWebhookError("Plaid webhook signature algorithm or key id is invalid")
        claims = _base64url_json(parts[1], "claims")
        self._validate_claims(claims, body)
        signature = _base64url_bytes(parts[2], "signature")
        if len(signature) != 64:
            raise PlaidWebhookError("Plaid webhook signature is invalid")
        key = self._verification_key(key_id)
        self._verify_es256(f"{parts[0]}.{parts[1]}".encode(), signature, key)

    def _verification_key(self, key_id: str) -> Mapping[str, object]:
        if not self.client_id or not self.client_secret_ref.startswith("secret://"):
            raise PlaidWebhookError("Plaid webhook verifier configuration is invalid")
        now = time.monotonic()
        with self._cache_lock:
            cached = self._key_cache.get(key_id)
            if cached and cached[0] > now:
                return cached[1]
            if cached:
                self._key_cache.pop(key_id, None)
            if self._negative_key_cache.get(key_id, 0) > now:
                raise PlaidWebhookError("Plaid webhook signature is invalid")
            self._negative_key_cache.pop(key_id, None)
            while self._key_fetches and self._key_fetches[0] <= now - 60:
                self._key_fetches.popleft()
            wait_for = self._inflight_keys.get(key_id)
            if wait_for is None:
                if len(self._key_fetches) >= self.max_key_fetches_per_minute:
                    raise PlaidWebhookError("Plaid webhook verification is temporarily rate limited")
                wait_for = Event()
                self._inflight_keys[key_id] = wait_for
                self._key_fetches.append(now)
                fetch_key = True
            else:
                fetch_key = False
        if not fetch_key:
            if not wait_for.wait(15):
                raise PlaidWebhookError("Plaid webhook verification key is unavailable")
            return self._verification_key(key_id)
        try:
            secret = self.secret_resolver.resolve(self.client_secret_ref)
            response = self.transport.request(
                "POST",
                f"{self.base_url.rstrip('/')}/webhook_verification_key/get",
                {},
                {"client_id": self.client_id, "secret": secret, "key_id": key_id},
            )
            if response.status_code >= 400:
                if 400 <= response.status_code < 500:
                    with self._cache_lock:
                        self._negative_key_cache[key_id] = time.monotonic() + min(60, self.verification_key_ttl_seconds)
                raise PlaidWebhookError("Plaid webhook verification key is unavailable")
            key = response.body.get("key")
            if not isinstance(key, Mapping):
                raise PlaidWebhookError("Plaid webhook verification key is invalid")
            with self._cache_lock:
                self._key_cache[key_id] = (time.monotonic() + self.verification_key_ttl_seconds, key)
            return key
        finally:
            with self._cache_lock:
                completed = self._inflight_keys.pop(key_id, None)
                if completed is not None:
                    completed.set()

    def _validate_claims(self, claims: Mapping[str, object], body: bytes) -> None:
        issued_at = claims.get("iat")
        body_hash = claims.get("request_body_sha256")
        if not isinstance(issued_at, int) or not isinstance(body_hash, str):
            raise PlaidWebhookError("Plaid webhook claims are invalid")
        now = int(time.time())
        if issued_at > now + 30 or now - issued_at > self.max_age_seconds:
            raise PlaidWebhookError("Plaid webhook signature is expired")
        if not hmac.compare_digest(body_hash, sha256(body).hexdigest()):
            raise PlaidWebhookError("Plaid webhook body hash does not match its signature")

    @staticmethod
    def _verify_es256(signing_input: bytes, signature: bytes, key: Mapping[str, object]) -> None:
        if key.get("kty") != "EC" or key.get("crv") != "P-256" or key.get("alg") != "ES256":
            raise PlaidWebhookError("Plaid webhook verification key is invalid")
        x, y = key.get("x"), key.get("y")
        if not isinstance(x, str) or not isinstance(y, str) or len(signature) != 64:
            raise PlaidWebhookError("Plaid webhook verification key is invalid")
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

            public_key = ec.EllipticCurvePublicNumbers(
                int.from_bytes(_base64url_bytes(x, "key x"), "big"),
                int.from_bytes(_base64url_bytes(y, "key y"), "big"),
                ec.SECP256R1(),
            ).public_key()
            der_signature = encode_dss_signature(
                int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
            )
            public_key.verify(der_signature, signing_input, ec.ECDSA(hashes.SHA256()))
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise PlaidWebhookError("install cryptography to verify Plaid webhooks") from exc
        except (InvalidSignature, ValueError) as exc:
            raise PlaidWebhookError("Plaid webhook signature is invalid") from exc
