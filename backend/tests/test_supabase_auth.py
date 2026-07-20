import unittest

from app.supabase_auth import (
    AuthResponse,
    AuthenticationError,
    AuthenticationUnavailable,
    SupabaseAuthConfig,
    SupabaseAuthVerifier,
)


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, headers))
        return self.response


class SupabaseAuthTests(unittest.TestCase):
    def test_configuration_requires_a_server_side_publishable_key(self):
        with self.assertRaises(AuthenticationUnavailable):
            SupabaseAuthConfig.from_environment({"SUPABASE_URL": "https://demo.supabase.co"})
        with self.assertRaises(AuthenticationUnavailable):
            SupabaseAuthConfig("http://example.test", "sb_publishable_key")

    def test_validates_user_with_the_auth_service(self):
        transport = FakeTransport(AuthResponse(200, {"id": "user-1", "email": "controller@example.test"}))
        verifier = SupabaseAuthVerifier(
            SupabaseAuthConfig("https://demo.supabase.co", "sb_publishable_key"), transport
        )
        user = verifier.authenticate("access-token")
        self.assertEqual(user.subject, "user-1")
        self.assertEqual(user.email, "controller@example.test")
        self.assertEqual(transport.calls[0][0], "https://demo.supabase.co/auth/v1/user")
        self.assertEqual(transport.calls[0][1]["Authorization"], "Bearer access-token")

    def test_reuses_a_short_lived_positive_auth_validation(self):
        transport = FakeTransport(AuthResponse(200, {"id": "user-1", "email": "controller@example.test"}))
        verifier = SupabaseAuthVerifier(
            SupabaseAuthConfig("https://demo.supabase.co", "sb_publishable_key", cache_seconds=15), transport
        )
        verifier.authenticate("access-token")
        verifier.authenticate("access-token")
        self.assertEqual(len(transport.calls), 1)

    def test_rejects_an_invalid_token(self):
        verifier = SupabaseAuthVerifier(
            SupabaseAuthConfig("https://demo.supabase.co", "sb_publishable_key"),
            FakeTransport(AuthResponse(401, {})),
        )
        with self.assertRaises(AuthenticationError):
            verifier.authenticate("expired")


if __name__ == "__main__":
    unittest.main()
