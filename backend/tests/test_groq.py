import unittest

from app.ai import AIValidationError, ExplanationContext, GroundedFact, GroundedExplanationService
from app.groq import GroqConfig, GroqError, GroqExplanationModel, GroqRateLimitError, GroqResponse


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, endpoint, headers, payload, timeout):
        self.calls.append((endpoint, headers, payload, timeout))
        return self.response


def response_body():
    return {
        "choices": [
            {
                "message": {
                    "content": '{"cause":"Mismatch","recommendation":"Review","evidence_ids":["e-1"],"uncertainties":[],"confidence_label":"low","amounts":[],"account_codes":[],"dates":[]}'
                }
            }
        ],
        "usage": {"total_tokens": 42},
    }


class GroqTests(unittest.TestCase):
    def test_config_rejects_public_credentials_and_uses_server_model(self):
        config = GroqConfig.from_environment({"GROQ_API_KEY": "secret-key"})
        self.assertEqual(config.model, "openai/gpt-oss-20b")
        with self.assertRaises(GroqError):
            GroqConfig.from_environment({"NEXT_PUBLIC_GROQ_API_KEY": "secret-key", "GROQ_API_KEY": "secret-key"})
        with self.assertRaises(GroqError):
            GroqConfig.from_environment({"GROQ_API_KEY": "secret-key", "GROQ_TIMEOUT_SECONDS": "fast"})

    def test_structured_response_is_sent_and_usage_is_recorded(self):
        transport = FakeTransport(GroqResponse(200, response_body(), {}))
        model = GroqExplanationModel(GroqConfig("server-key"), transport)
        context = ExplanationContext("exception-1", (GroundedFact("e-1", "status", "open"),))
        result = GroundedExplanationService(model).explain(context)
        self.assertEqual(result.cause, "Mismatch")
        self.assertEqual(model.last_usage["total_tokens"], 42)
        payload = transport.calls[0][2]
        self.assertEqual(payload["response_format"]["json_schema"]["strict"], True)
        self.assertNotIn("server-key", str(payload))

    def test_rate_limit_fails_closed_after_bounded_retry(self):
        transport = FakeTransport(GroqResponse(429, {"error": {"message": "rate limit"}}, {}))
        model = GroqExplanationModel(GroqConfig("server-key"), transport)
        context = ExplanationContext("exception-1", (GroundedFact("e-1", "status", "open"),))
        with self.assertRaises(AIValidationError):
            GroundedExplanationService(model).explain(context)
        self.assertEqual(len(transport.calls), 2)

    def test_invalid_structured_content_is_rejected(self):
        transport = FakeTransport(GroqResponse(200, {"choices": [{"message": {"content": "not-json"}}]}, {}))
        model = GroqExplanationModel(GroqConfig("server-key"), transport)
        with self.assertRaises(GroqError):
            model.generate("bounded prompt", "1")


if __name__ == "__main__":
    unittest.main()
