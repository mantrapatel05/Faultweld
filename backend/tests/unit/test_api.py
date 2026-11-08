from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import TestCase

from fastapi.testclient import TestClient

from app.api.application import create_app
from app.api.runtime import ApiRuntime
from app.policy.principals import LOCAL_DEMO_USER_ID


class ApiContractTests(TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(ignore_cleanup_errors=True)
        database = Path(self.directory.name) / "api.sqlite3"
        self.runtime = ApiRuntime(database)
        self.app = create_app(self.runtime)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.directory.cleanup()

    def run_healthy_scenario(self) -> dict[str, Any]:
        response = self.client.post("/api/scenarios/healthy-replica-loss/runs", json={})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["policy_decision"], "require_approval")
        self.assertIsNotNone(payload["proposal_id"])
        return cast(dict[str, Any], payload)

    def approve(self, proposal_id: str, version: int) -> dict[str, object]:
        response = self.client.post(
            f"/api/proposals/{proposal_id}/decision",
            json={"decision": "approve", "expected_version": version},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return cast(dict[str, object], response.json())

    def test_health_and_readiness_reflect_the_configured_runtime(self) -> None:
        for path in ("/health", "/health/live", "/health/ready"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"service": "backend", "status": "ok"})

    def test_scenario_proposal_approval_execution_and_verification(self) -> None:
        run = self.run_healthy_scenario()
        proposal_id = str(run["proposal_id"])
        incident_id = str(run["incident_id"])

        proposal = self.client.get(f"/api/proposals/{proposal_id}")
        self.assertEqual(proposal.status_code, 200, proposal.text)
        proposal_body = proposal.json()
        self.assertEqual(proposal_body["action_type"], "scale_deployment")
        self.assertEqual(proposal_body["proposed_replicas"], 1)
        self.assertFalse(proposal_body["state_changed"])
        self.assertTrue(proposal_body["evidence_ids"])
        self.assertTrue(proposal_body["preconditions"])

        approval = self.approve(proposal_id, int(run["proposal_version"]))
        self.assertEqual(approval["approver_principal_id"], LOCAL_DEMO_USER_ID)
        self.assertEqual(approval["decision"], "approve")

        execution = self.client.post(f"/api/proposals/{proposal_id}/execute")
        self.assertEqual(execution.status_code, 200, execution.text)
        self.assertTrue(execution.json()["executed"])
        self.assertEqual(execution.json()["verification_status"], "recovered")
        self.assertEqual(execution.json()["writer_operations"], 1)

        incident = self.client.get(f"/api/incidents/{incident_id}")
        self.assertEqual(incident.status_code, 200, incident.text)
        self.assertEqual(incident.json()["status"], "closed")
        self.assertEqual(incident.json()["verification_status"], "recovered")
        self.assertTrue(incident.json()["timeline"])

    def test_stale_state_after_approval_is_refused_by_gateway(self) -> None:
        run = self.run_healthy_scenario()
        proposal_id = str(run["proposal_id"])
        self.approve(proposal_id, int(run["proposal_version"]))
        before = self.runtime.cluster.state
        self.runtime.cluster.mutate_state(readiness_path="/changed-by-another-operator")
        after_mutation = self.runtime.cluster.state

        response = self.client.post(f"/api/proposals/{proposal_id}/execute")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "state_changed")
        self.assertNotEqual(after_mutation, before)
        self.assertEqual(self.runtime.cluster.state, after_mutation)

    def test_approval_rejects_state_changed_before_decision(self) -> None:
        run = self.run_healthy_scenario()
        proposal_id = str(run["proposal_id"])
        self.runtime.cluster.mutate_state(readiness_port=8080)

        response = self.client.post(
            f"/api/proposals/{proposal_id}/decision",
            json={"decision": "approve", "expected_version": int(run["proposal_version"])},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "state_changed_since_proposal")

    def test_rejection_requires_reason_and_does_not_execute(self) -> None:
        run = self.run_healthy_scenario()
        proposal_id = str(run["proposal_id"])
        version = int(run["proposal_version"])

        invalid = self.client.post(
            f"/api/proposals/{proposal_id}/decision",
            json={"decision": "reject", "expected_version": version},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "request_validation_error")

        rejected = self.client.post(
            f"/api/proposals/{proposal_id}/decision",
            json={"decision": "reject", "expected_version": version, "reason": "not approved"},
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["decision"], "reject")
        execute = self.client.post(f"/api/proposals/{proposal_id}/execute")
        self.assertEqual(execute.status_code, 409)
        self.assertEqual(execute.json()["code"], "approval_rejected")

    def test_approver_identity_cannot_be_supplied_by_the_request(self) -> None:
        run = self.run_healthy_scenario()
        proposal_id = str(run["proposal_id"])
        response = self.client.post(
            f"/api/proposals/{proposal_id}/decision",
            json={
                "decision": "approve",
                "expected_version": int(run["proposal_version"]),
                "approver_principal_id": "attacker-controlled",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "request_validation_error")

    def test_expected_version_uses_optimistic_concurrency(self) -> None:
        run = self.run_healthy_scenario()
        proposal_id = str(run["proposal_id"])
        response = self.client.post(
            f"/api/proposals/{proposal_id}/decision",
            json={"decision": "approve", "expected_version": 1},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "stale_version")

    def test_expected_version_rejects_boolean_values(self) -> None:
        run = self.run_healthy_scenario()
        proposal_id = str(run["proposal_id"])
        response = self.client.post(
            f"/api/proposals/{proposal_id}/decision",
            json={"decision": "approve", "expected_version": True},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "request_validation_error")

    def test_unhealthy_after_execution_is_not_reported_as_recovered(self) -> None:
        run = self.client.post(
            "/api/scenarios/readiness-regression/runs", json={}
        )
        self.assertEqual(run.status_code, 200, run.text)
        payload = run.json()
        proposal_id = str(payload["proposal_id"])
        incident_id = str(payload["incident_id"])
        self.approve(proposal_id, int(payload["proposal_version"]))

        execution = self.client.post(f"/api/proposals/{proposal_id}/execute")
        self.assertEqual(execution.status_code, 200, execution.text)
        self.assertTrue(execution.json()["executed"])
        self.assertEqual(execution.json()["verification_status"], "not_recovered")
        incident = self.client.get(f"/api/incidents/{incident_id}")
        self.assertEqual(incident.json()["status"], "escalated")
        self.assertEqual(incident.json()["verification_status"], "not_recovered")

    def test_incident_listing_uses_cursor_pagination(self) -> None:
        self.run_healthy_scenario()
        self.run_healthy_scenario()

        first = self.client.get("/api/incidents?limit=1")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(len(first.json()["items"]), 1)
        cursor = first.json()["next_cursor"]
        self.assertIsNotNone(cursor)

        second = self.client.get(f"/api/incidents?limit=1&cursor={cursor}")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(second.json()["items"]), 1)
        self.assertNotEqual(
            first.json()["items"][0]["incident_id"],
            second.json()["items"][0]["incident_id"],
        )

    def test_malformed_cursor_uses_problem_details(self) -> None:
        response = self.client.get("/api/incidents?cursor=not-a-valid-cursor")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(response.json()["code"], "invalid_cursor")

    def test_missing_resources_return_stable_problem_details(self) -> None:
        for path, code in (
            ("/api/incidents/missing", "incident_not_found"),
            ("/api/proposals/missing", "proposal_not_found"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers["content-type"], "application/problem+json")
                self.assertEqual(response.json()["code"], code)

    def test_unknown_scenario_is_a_stable_not_found_problem(self) -> None:
        response = self.client.post("/api/scenarios/does-not-exist/runs", json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "scenario_not_found")

    def test_demo_mutation_route_is_not_mounted_in_the_normal_app(self) -> None:
        schema = self.client.get("/openapi.json").json()
        self.assertNotIn("/api/demo/{incident_id}/mutate", schema["paths"])
        response = self.client.post("/api/demo/incident-1/mutate", json={"readiness_port": 8080})
        self.assertEqual(response.status_code, 404)

    def test_demo_app_exposes_the_mutation_route_and_persists_it(self) -> None:
        run = self.run_healthy_scenario()
        incident_id = str(run["incident_id"])
        proposal_id = str(run["proposal_id"])
        self.approve(proposal_id, int(run["proposal_version"]))

        # Model the documented process boundary: the demo app and the restarted normal
        # app each construct their own runtime while sharing only the SQLite database.
        demo_runtime = ApiRuntime(self.runtime.db_path)
        demo_client = TestClient(create_app(demo_runtime, include_demo_routes=True))
        try:
            schema = demo_client.get("/openapi.json").json()
            self.assertIn("/api/demo/{incident_id}/mutate", schema["paths"])
            response = demo_client.post(
                f"/api/demo/{incident_id}/mutate",
                json={"readiness_port": 8080},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["readiness_port"], "8080")
            self.assertNotEqual(response.json()["state_fingerprint"], "0" * 64)
        finally:
            demo_client.close()

        restarted_runtime = ApiRuntime(self.runtime.db_path)
        restarted_client = TestClient(create_app(restarted_runtime))
        try:
            before_execute = restarted_runtime.cluster.source.snapshot()
            execution = restarted_client.post(f"/api/proposals/{proposal_id}/execute")
            self.assertEqual(execution.status_code, 409, execution.text)
            self.assertEqual(execution.json()["code"], "state_changed")
            after_execute = restarted_runtime.cluster.source.snapshot()
            self.assertEqual(after_execute, before_execute)
        finally:
            restarted_client.close()
        with restarted_runtime.open_store() as store:
            stored = store.get_proposal(proposal_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertIsNone(store.get_attempt_by_key(stored.proposal.idempotency_key))
            approval = store.get_approval_for_proposal(proposal_id)
            self.assertIsNotNone(approval)
            assert approval is not None
            self.assertFalse(approval.approval.consumed)

    def test_only_allowlisted_mutating_routes_appear_in_openapi(self) -> None:
        schema = self.client.get("/openapi.json").json()
        mutating = {
            (path, method)
            for path, operations in schema["paths"].items()
            for method in operations
            if method.lower() in {"post", "put", "patch", "delete"}
        }
        self.assertEqual(
            mutating,
            {
                ("/api/proposals/{proposal_id}/decision", "post"),
                ("/api/proposals/{proposal_id}/execute", "post"),
                ("/api/scenarios/{scenario_id}/runs", "post"),
            },
        )

    def test_unallowlisted_methods_return_problem_details(self) -> None:
        requests = (
            ("post", "/api/incidents", 405),
            ("put", "/api/incidents/incident-1", 405),
            ("patch", "/api/incidents/incident-1", 405),
            ("delete", "/api/incidents/incident-1", 405),
            ("post", "/api/proposals/proposal-1/approve", 404),
            ("post", "/api/incidents/incident-1/verify", 404),
        )
        for method, path, expected_status in requests:
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.headers["content-type"], "application/problem+json")

    def test_openapi_has_flat_contracts_and_problem_details(self) -> None:
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(schema["info"]["version"], "0.2.0")
        self.assertIn("/api/incidents", schema["paths"])
        self.assertIn("/api/incidents/{incident_id}", schema["paths"])
        self.assertIn("/api/proposals/{proposal_id}", schema["paths"])
        self.assertIn("/api/proposals/{proposal_id}/decision", schema["paths"])
        self.assertIn("/api/proposals/{proposal_id}/execute", schema["paths"])
        self.assertIn("/api/scenarios/{scenario_id}/runs", schema["paths"])
        self.assertEqual(
            set(schema["components"]["schemas"]["ProblemDetails"]["properties"]),
            {"type", "title", "status", "detail", "instance", "code"},
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
