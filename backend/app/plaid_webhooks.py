"""Verified Plaid webhook handling using Plaid's signed JWT envelope."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256
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

    def verify(self, signed_header: str, body: bytes) -> None:
        parts = signed_header.split(".")
        if len(parts) != 3:
            raise PlaidWebhookError("Plaid webhook signature is invalid")
        header = _base64url_json(parts[0], "header")
        key_id = header.get("kid")
        if header.get("alg") != "ES256" or not isinstance(key_id, str) or not key_id:
            raise PlaidWebhookError("Plaid webhook signature algorithm or key id is invalid")
        claims = _base64url_json(parts[1], "claims")
        self._validate_claims(claims, body)
        key = self._verification_key(key_id)
        self._verify_es256(f"{parts[0]}.{parts[1]}".encode(), _base64url_bytes(parts[2], "signature"), key)

    def _verification_key(self, key_id: str) -> Mapping[str, object]:
        if not self.client_id or not self.client_secret_ref.startswith("secret://"):
            raise PlaidWebhookError("Plaid webhook verifier configuration is invalid")
        secret = self.secret_resolver.resolve(self.client_secret_ref)
        response = self.transport.request(
            "POST",
            f"{self.base_url.rstrip('/')}/webhook_verification_key/get",
            {},
            {"client_id": self.client_id, "secret": secret, "key_id": key_id},
        )
        if response.status_code >= 400:
            raise PlaidWebhookError("Plaid webhook verification key is unavailable")
        key = response.body.get("key")
        if not isinstance(key, Mapping):
            raise PlaidWebhookError("Plaid webhook verification key is invalid")
        return key

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
