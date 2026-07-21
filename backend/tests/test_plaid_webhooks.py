import base64
import json
import time
import unittest
from hashlib import sha256
from unittest.mock import patch

from app.plaid_webhooks import PlaidWebhookError, PlaidWebhookVerifier
from app.provider_runtime import JsonResponse, StaticSecretResolver


def token_part(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers, payload=None):
        self.calls.append((method, url, headers, payload))
        return JsonResponse(
            200,
            {"key": {"kty": "EC", "crv": "P-256", "alg": "ES256", "x": "x", "y": "y"}},
            {},
        )


class PlaidWebhookTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.verifier = PlaidWebhookVerifier(
            "plaid-client",
            "secret://plaid/production/client-secret",
            StaticSecretResolver({"secret://plaid/production/client-secret": "plaid-secret"}),
            self.transport,
        )

    def test_signed_header_binds_the_exact_body_and_fetches_the_claimed_key(self):
        body = b'{"request_id":"evt-1"}'
        header = token_part({"alg": "ES256", "kid": "key-1"})
        claims = token_part({"iat": int(time.time()), "request_body_sha256": sha256(body).hexdigest()})
        signature = base64.urlsafe_b64encode(b"a" * 64).decode().rstrip("=")
        with patch.object(PlaidWebhookVerifier, "_verify_es256") as verify_signature:
            self.verifier.verify(f"{header}.{claims}.{signature}", body)
        self.assertEqual(self.transport.calls[0][1], "https://production.plaid.com/webhook_verification_key/get")
        self.assertEqual(self.transport.calls[0][3]["key_id"], "key-1")
        verify_signature.assert_called_once()

    def test_body_tampering_is_rejected_before_a_key_is_requested(self):
        header = token_part({"alg": "ES256", "kid": "key-1"})
        claims = token_part({"iat": int(time.time()), "request_body_sha256": "0" * 64})
        signature = base64.urlsafe_b64encode(b"a" * 64).decode().rstrip("=")
        with self.assertRaisesRegex(PlaidWebhookError, "body hash"):
            self.verifier.verify(f"{header}.{claims}.{signature}", b'{"request_id":"evt-1"}')
        self.assertEqual(self.transport.calls, [])

    def test_verification_key_is_cached_after_a_valid_lookup(self):
        body = b'{"request_id":"evt-1"}'
        header = token_part({"alg": "ES256", "kid": "key-1"})
        claims = token_part({"iat": int(time.time()), "request_body_sha256": sha256(body).hexdigest()})
        signature = base64.urlsafe_b64encode(b"a" * 64).decode().rstrip("=")
        with patch.object(PlaidWebhookVerifier, "_verify_es256"):
            self.verifier.verify(f"{header}.{claims}.{signature}", body)
            self.verifier.verify(f"{header}.{claims}.{signature}", body)
        self.assertEqual(len(self.transport.calls), 1)

    def test_unknown_key_lookups_are_rate_limited(self):
        verifier = PlaidWebhookVerifier(
            "plaid-client",
            "secret://plaid/production/client-secret",
            StaticSecretResolver({"secret://plaid/production/client-secret": "plaid-secret"}),
            self.transport,
            max_key_fetches_per_minute=1,
        )
        body = b'{"request_id":"evt-1"}'
        claims = token_part({"iat": int(time.time()), "request_body_sha256": sha256(body).hexdigest()})
        signature = base64.urlsafe_b64encode(b"a" * 64).decode().rstrip("=")
        with patch.object(PlaidWebhookVerifier, "_verify_es256"):
            verifier.verify(f"{token_part({'alg': 'ES256', 'kid': 'key-1'})}.{claims}.{signature}", body)
        with self.assertRaisesRegex(PlaidWebhookError, "rate limited"):
            verifier.verify(f"{token_part({'alg': 'ES256', 'kid': 'key-2'})}.{claims}.{signature}", body)
        self.assertEqual(len(self.transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
