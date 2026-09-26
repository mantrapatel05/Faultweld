from unittest import TestCase

from fastapi.testclient import TestClient

from app.main import app


class ApiContractTests(TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_health_and_liveness_are_available(self) -> None:
        for path in ("/health", "/health/live"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"service": "backend", "status": "ok"})

    def test_readiness_reports_unavailable_persistence(self) -> None:
        response = self.client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(
            response.json(),
            {
                "type": "urn:faultweld:problem:readiness",
                "title": "Readiness check failed",
                "status": 503,
                "detail": "Persistence is not configured for this foundation build.",
                "instance": "/health/ready",
                "code": "dependency_unavailable",
            },
        )

    def test_read_only_incident_contract(self) -> None:
        list_response = self.client.get("/api/incidents")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json(), {"items": [], "next_cursor": None})

        detail_response = self.client.get("/api/incidents/incident-1")
        self.assertEqual(detail_response.status_code, 404)
        self.assertEqual(detail_response.headers["content-type"], "application/problem+json")
        self.assertEqual(detail_response.json()["code"], "incident_not_found")

    def test_malformed_query_uses_problem_details(self) -> None:
        response = self.client.get("/api/incidents?limit=0")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(response.json()["code"], "request_validation_error")
        self.assertEqual(response.json()["instance"], "/api/incidents")

    def test_not_found_problem_details_are_stable(self) -> None:
        response = self.client.get("/api/incidents/missing")

        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(
            response.json(),
            {
                "type": "urn:faultweld:problem:not-found",
                "title": "Incident not found",
                "status": 404,
                "detail": "The requested incident does not exist in the local read-only store.",
                "instance": "/api/incidents/missing",
                "code": "incident_not_found",
            },
        )

    def test_method_not_allowed_problem_details_are_stable(self) -> None:
        response = self.client.post("/api/incidents")

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(
            response.json(),
            {
                "type": "urn:faultweld:problem:method-not-allowed",
                "title": "Method not allowed",
                "status": 405,
                "detail": "The requested method is not allowed for this resource.",
                "instance": "/api/incidents",
                "code": "method_not_allowed",
            },
        )

    def test_openapi_contains_read_only_paths(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema["info"]["version"], "0.1.3")
        self.assertIn("/health/live", schema["paths"])
        self.assertIn("/health/ready", schema["paths"])
        self.assertIn("/api/incidents", schema["paths"])
        self.assertIn("/api/incidents/{incident_id}", schema["paths"])
        self.assertEqual(
            set(schema["components"]["schemas"]["ProblemDetails"]["properties"]),
            {"type", "title", "status", "detail", "instance", "code"},
        )
        self.assertEqual(
            schema["components"]["schemas"]["IncidentSummary"]["required"],
            ["incident_id", "scenario_id", "status", "opened_at", "updated_at"],
        )
        for operations in schema["paths"].values():
            for method in operations:
                self.assertNotIn(method, {"post", "put", "patch", "delete"})

    def test_mutating_methods_are_not_exposed(self) -> None:
        requests = (
            ("post", "/api/incidents", 405),
            ("put", "/api/incidents/incident-1", 405),
            ("patch", "/api/incidents/incident-1", 405),
            ("delete", "/api/incidents/incident-1", 405),
            ("post", "/api/incidents/incident-1/decision", 404),
            ("post", "/api/incidents/incident-1/stop", 404),
        )
        for method, path, expected_status in requests:
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.headers["content-type"], "application/problem+json")
