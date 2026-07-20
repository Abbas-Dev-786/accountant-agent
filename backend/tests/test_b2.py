import unittest
from unittest.mock import patch

from app.b2 import B2Config, B2ObjectLockClient, B2Response


class FakeSecrets:
    def resolve(self, reference):
        return {"secret://b2/key-id": "key-id", "secret://b2/application-key": "application-key"}[reference]


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        if len(self.calls) == 1:
            return B2Response(200, {"authorizationToken": "account-token", "apiUrl": "https://api.example", "accountId": "account", "allowed": {"bucketId": "bucket"}})
        if len(self.calls) == 2:
            return B2Response(200, {"uploadUrl": "https://upload.example", "authorizationToken": "upload-token"})
        return B2Response(200, {"fileId": "file-1", "fileRetention": {"mode": "compliance"}})


class B2Tests(unittest.TestCase):
    def test_close_package_is_content_addressed_and_requires_compliance_lock(self):
        transport = FakeTransport()
        with patch("app.b2.secret_store_from_environment", return_value=FakeSecrets()):
            client = B2ObjectLockClient(B2Config("close-packages", "secret://b2/key-id", "secret://b2/application-key", 30), transport=transport)
            artifact = client.upload_close_package(run_id="run-1", package={"run_id": "run-1", "status": "review_frozen"})
        self.assertIn("run-1", artifact.object_key)
        self.assertEqual(artifact.file_id, "file-1")
        headers = transport.calls[-1][2]
        self.assertEqual(headers["X-Bz-File-Retention-Mode"], "compliance")
        self.assertIn("X-Bz-File-Retention-Retain-Until-Timestamp", headers)


if __name__ == "__main__":
    unittest.main()
