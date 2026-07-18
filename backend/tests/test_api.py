import unittest

from fastapi.testclient import TestClient

from app.main import app, service


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        service.runs.clear()
        self.client = TestClient(app)

    def test_health_discloses_the_synthetic_demo_boundary(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "mode": "demo", "data_class": "synthetic"})

    def test_new_run_starts_in_synchronizing_state(self) -> None:
        response = self.client.post(
            "/api/v1/close-runs",
            json={"organization_id": "demo-org", "period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        self.assertEqual(response.status_code, 201)
        run = self.client.get(f"/api/v1/close-runs/{response.json()['id']}")
        self.assertEqual(run.status_code, 200)
        self.assertEqual(run.json()["status"], "synchronizing")
        self.assertEqual(run.json()["deployment"], {"mode": "demo", "data_class": "synthetic"})


if __name__ == "__main__":
    unittest.main()
