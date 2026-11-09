"""Chromium checks for the Faultweld console, including live simulator workflows."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from typing import Any

from playwright.sync_api import (
    ConsoleMessage,
    Page,
    Request,
    Response,
    Route,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)

BASE_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
API_BASE_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
INDEX_ROUTE = re.compile(r"/api/incidents(?:\?.*)?$")
DETAIL_ROUTE = re.compile(r"/api/incidents/[^/?#]+$")
PROPOSAL_ROUTE = re.compile(r"/api/proposals/[^/?#]+$")
SCENARIO_ROUTE = re.compile(r"/api/scenarios/[^/?#]+/runs$")
DECISION_ROUTE = re.compile(r"/api/proposals/[^/?#]+/decision$")
EXECUTE_ROUTE = re.compile(r"/api/proposals/[^/?#]+/execute$")

INCIDENT: dict[str, Any] = {
    "incident_id": "inc-1",
    "scenario_id": "healthy-replica-loss",
    "status": "awaiting_approval",
    "opened_at": "2026-02-01T09:00:00Z",
    "updated_at": "2026-02-01T09:30:00Z",
    "environment_id": "faultweld-lite",
    "current_run_id": "run-1",
    "terminal_reason": None,
    "verification_status": None,
    "proposal_ids": ["proposal-1"],
    "timeline": [
        {
            "event_id": 1,
            "occurred_at": "2026-02-01T09:30:00Z",
            "kind": "proposal_recorded",
            "subject": "proposal-1",
            "detail": {"proposal_id": "proposal-1", "policy_decision": "require_approval"},
        }
    ],
}

PROPOSAL: dict[str, Any] = {
    "proposal_id": "proposal-1",
    "incident_id": "inc-1",
    "run_id": "run-1",
    "version": 2,
    "state": "awaiting_approval",
    "action_type": "scale_deployment",
    "target_cluster_id": "faultweld",
    "target_namespace": "faultweld-demo",
    "target_kind": "Deployment",
    "target_name": "faultweld-sample",
    "target_uid": "uid-1",
    "proposed_replicas": 1,
    "proposed_revision": None,
    "current_desired_replicas": 2,
    "current_available_replicas": 1,
    "current_ready_replicas": 1,
    "current_revision": 1,
    "current_resource_version": 7,
    "current_readiness_path": "/",
    "current_readiness_port": "8080",
    "expected_state_fingerprint": "a" * 64,
    "current_state_fingerprint": "a" * 64,
    "state_observed_at": "2026-02-01T09:29:00Z",
    "current_state_observed_at": "2026-02-01T09:30:00Z",
    "state_changed": False,
    "preconditions": [
        {
            "name": "live_state_fingerprint",
            "expected": "a" * 64,
            "observed": "a" * 64,
            "passed": True,
            "checked_at": "2026-02-01T09:30:00Z",
        }
    ],
    "expected_postconditions": ["ready replicas equal desired replicas"],
    "evidence_ids": ["evidence-1"],
    "policy_decision": "require_approval",
    "policy_reason_codes": ["approval_required"],
    "policy_version": "1",
    "policy_bundle_digest": "b" * 64,
    "expires_at": "2099-02-01T09:30:00Z",
}

RUN_RESULT: dict[str, Any] = {
    "scenario_id": "healthy-replica-loss",
    "run_id": "run-1",
    "incident_id": "inc-1",
    "incident_status": "awaiting_approval",
    "investigation_status": "proposed",
    "escalation_reason": None,
    "proposal_id": "proposal-1",
    "proposal_version": 2,
    "policy_decision": "require_approval",
    "policy_reason_codes": ["approval_required"],
    "model_calls": 2,
    "tokens_used": 120,
}


class CheckFailure(AssertionError):
    """Raised when a browser assertion fails."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def fulfill(route: Route, body: Any, status: int = 200) -> None:
    media_type = "application/problem+json" if status >= 400 else "application/json"
    route.fulfill(status=status, content_type=media_type, body=json.dumps(body))


def reset_routes(page: Page) -> None:
    patterns = (
        INDEX_ROUTE,
        DETAIL_ROUTE,
        PROPOSAL_ROUTE,
        SCENARIO_ROUTE,
        DECISION_ROUTE,
        EXECUTE_ROUTE,
    )
    for pattern in patterns:
        page.unroute(pattern)


def stub_index(page: Page, body: Any, status: int = 200) -> None:
    page.route(INDEX_ROUTE, lambda route: fulfill(route, body, status))


def stub_workspace(page: Page, incident: dict[str, Any], proposal: dict[str, Any] | None) -> None:
    page.route(DETAIL_ROUTE, lambda route: fulfill(route, incident))
    if proposal is not None:
        page.route(PROPOSAL_ROUTE, lambda route: fulfill(route, proposal))


def goto(page: Page, path: str) -> None:
    page.goto(f"{BASE_URL}{path}")
    page.wait_for_load_state("networkidle")


def check_document_shell(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})
    goto(page, "/overview")
    page.get_by_role("heading", name="No incidents yet").wait_for()
    require(page.title() == "Faultweld", "document title must remain Faultweld")
    require(page.evaluate("document.documentElement.lang") == "en", "document language must be en")
    require(page.get_by_role("banner").count() == 1, "expected one banner landmark")
    require(page.get_by_role("main").count() == 1, "expected one main landmark")
    require(
        page.get_by_role("navigation", name="Primary navigation").count() == 1,
        "primary navigation is missing",
    )
    require(page.locator("main h1").count() == 1, "expected one main heading")


def check_scenario_run_and_approval(page: Page) -> None:
    state: dict[str, Any] = {"incident": dict(INCIDENT), "proposal": dict(PROPOSAL), "ran": False}
    stub_index(page, {"items": [], "next_cursor": None})

    def run_scenario(route: Route) -> None:
        state["ran"] = True
        fulfill(route, RUN_RESULT)

    def get_incident(route: Route) -> None:
        fulfill(route, state["incident"])

    def get_proposal(route: Route) -> None:
        fulfill(route, state["proposal"])

    def decide(route: Route) -> None:
        body = route.request.post_data_json
        require(
            isinstance(body, dict) and body.get("decision") == "approve",
            "approval request must explicitly choose approve",
        )
        state["proposal"] = {**state["proposal"], "state": "authorized", "version": 3}
        fulfill(
            route,
            {
                "proposal_id": "proposal-1",
                "approval_id": "approval-1",
                "decision": "approve",
                "approver_principal_id": "local-demo-user",
                "approver_role": "operator",
                "expires_at": "2099-02-01T09:30:00Z",
                "proposal_version": 3,
                "already_decided": False,
            },
        )

    page.route(SCENARIO_ROUTE, run_scenario)
    page.route(DETAIL_ROUTE, get_incident)
    page.route(PROPOSAL_ROUTE, get_proposal)
    page.route(DECISION_ROUTE, decide)

    goto(page, "/overview")
    page.get_by_role("button", name="Run scenario").click()
    page.get_by_role("heading", name="Action proposal").wait_for()
    require(bool(state["ran"]), "scenario run endpoint was not called")
    page.get_by_role("button", name="Approve proposal").click()
    page.get_by_role("button", name="Execute approved action").wait_for()
    require(state["proposal"]["state"] == "authorized", "approval must refresh proposal state")


def check_incident_timeline_and_evidence(page: Page) -> None:
    stub_workspace(page, INCIDENT, PROPOSAL)
    goto(page, "/incidents/inc-1")
    page.get_by_role("heading", name="Timeline").wait_for()
    require(
        page.get_by_text("proposal recorded", exact=True).count() == 1,
        "audit event kind is missing",
    )
    require(
        page.get_by_text("evidence-1", exact=True).count() == 1,
        "proposal evidence reference is missing",
    )
    require(
        page.get_by_text("raw evidence payloads", exact=False).count() == 1,
        "evidence limitation must be explicit",
    )
    require(
        page.get_by_role("button", name="Approve proposal").count() == 1,
        "approval control is missing",
    )


def check_unknown_verification_is_not_recovery(page: Page) -> None:
    state = {"proposal": {**PROPOSAL, "state": "authorized", "version": 3}}
    incident = {**INCIDENT, "status": "escalated", "verification_status": "unknown"}

    def get_incident(route: Route) -> None:
        fulfill(route, incident)

    def get_proposal(route: Route) -> None:
        fulfill(route, state["proposal"])

    def execute(route: Route) -> None:
        fulfill(
            route,
            {
                "proposal_id": "proposal-1",
                "reason_code": "execution_unknown",
                "executed": True,
                "attempt_id": "attempt-1",
                "attempt_outcome": "executed",
                "proposal_state": "escalated",
                "verification_status": "unknown",
                "writer_operations": 1,
            },
            202,
        )

    page.route(DETAIL_ROUTE, get_incident)
    page.route(PROPOSAL_ROUTE, get_proposal)
    page.route(EXECUTE_ROUTE, execute)
    goto(page, "/incidents/inc-1")
    page.get_by_role("button", name="Execute approved action").click()
    page.get_by_role("heading", name="Verification outcome is unknown").wait_for()
    require(
        page.get_by_text("unknown", exact=True).count() >= 1,
        "unknown verification state must remain visible",
    )
    require(
        page.get_by_role("heading", name="Verifier confirmed recovery").count() == 0,
        "unknown must not be rendered as recovered",
    )


def check_problem_details(page: Page) -> None:
    problem: dict[str, str | int] = {
        "type": "urn:faultweld:problem:readiness",
        "title": "Readiness check failed",
        "status": 503,
        "detail": "Persistence is temporarily unavailable.",
        "instance": "/api/incidents",
        "code": "dependency_unavailable",
    }
    stub_index(page, problem, status=503)
    goto(page, "/overview")
    alert = page.get_by_role("alert")
    alert.wait_for()
    require(
        alert.get_by_text(str(problem["detail"])).count() == 1,
        "Problem Details body should be rendered",
    )
    require(
        page.get_by_text("API unavailable", exact=True).count() == 1,
        "connection state should be explicit",
    )


def check_keyboard_navigation(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})
    goto(page, "/overview")
    page.get_by_role("heading", name="No incidents yet").wait_for()
    brand = page.get_by_role("link", name="Faultweld overview")
    brand.focus()
    page.keyboard.press("Tab")
    require(
        page.locator(":focus").get_attribute("href") == "/overview",
        "overview must follow the brand in tab order",
    )
    outline = page.eval_on_selector(":focus", "element => getComputedStyle(element).outlineStyle")
    require(outline != "none", "keyboard focus outline must remain visible")
    page.keyboard.press("Tab")
    require(
        page.locator(":focus").get_attribute("href") == "/incidents",
        "workspace must follow overview in tab order",
    )


def check_route_not_found(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})
    goto(page, "/route-does-not-exist")
    page.get_by_role("heading", name="That surface is not wired.").wait_for()
    require(
        page.get_by_role("link", name="Go to overview").count() == 1,
        "not-found route needs a recovery link",
    )


def live_json(page: Page, method: str, path: str, data: dict[str, object] | None = None) -> Any:
    response = page.request.fetch(f"{API_BASE_URL}{path}", method=method, data=data)
    if not response.ok:
        raise CheckFailure(f"{method} {path} returned {response.status}: {response.text()}")
    return response.json()


def start_live_scenario(page: Page) -> tuple[str, str]:
    goto(page, "/overview")
    page.locator(".scenario-launcher .scenario-form label:first-child select").select_option(
        "healthy-replica-loss"
    )
    page.get_by_role("button", name="Run scenario").click()
    page.get_by_role("heading", name="Action proposal").wait_for()
    incident_id = page.url.rsplit("/", maxsplit=1)[-1]
    incident = live_json(page, "GET", f"/api/incidents/{incident_id}")
    proposal_ids = incident.get("proposal_ids")
    require(
        isinstance(proposal_ids, list) and bool(proposal_ids),
        "live run did not create a proposal",
    )
    return incident_id, str(proposal_ids[0])


def check_live_operator_flows(page: Page) -> None:
    incident_id, proposal_id = start_live_scenario(page)
    page.get_by_role("button", name="Approve proposal").click()
    page.get_by_role("button", name="Execute approved action").wait_for()
    page.get_by_role("button", name="Execute approved action").click()
    page.get_by_role("heading", name="Verifier confirmed recovery").wait_for()
    incident = live_json(page, "GET", f"/api/incidents/{incident_id}")
    require(incident["status"] == "closed", "approved live action did not close the incident")
    require(
        incident["verification_status"] == "recovered",
        "live verifier did not confirm recovery",
    )
    page.reload()
    page.wait_for_load_state("networkidle")
    page.get_by_text("closed", exact=True).wait_for()
    require(page.get_by_text("recovered", exact=True).count() >= 1, "reload lost verified state")

    incident_id, proposal_id = start_live_scenario(page)
    before = live_json(page, "GET", f"/api/proposals/{proposal_id}")
    page.get_by_label("Rejection reason").fill("Operator declined this change")
    page.get_by_role("button", name="Reject proposal").click()
    page.get_by_role("heading", name="Policy denied this proposal").wait_for()
    require(
        page.get_by_role("button", name="Execute approved action").count() == 0,
        "rejected live proposal exposed execution",
    )
    after = live_json(page, "GET", f"/api/proposals/{proposal_id}")
    require(after["state"] == "denied", "the API did not persist the rejection")
    require(
        after["current_desired_replicas"] == before["current_desired_replicas"],
        "rejection changed simulator replicas",
    )
    incident = live_json(page, "GET", f"/api/incidents/{incident_id}")
    require(incident["status"] == "escalated", "rejected incident status was not persisted")

    _, proposal_id = start_live_scenario(page)
    proposal = live_json(page, "GET", f"/api/proposals/{proposal_id}")
    incident_id = str(proposal["incident_id"])
    mutation = page.request.post(
        f"{API_BASE_URL}/api/demo/{incident_id}/mutate",
        data={"readiness_path": "/changed-during-review"},
    )
    require(mutation.ok, f"demo state mutation returned {mutation.status}: {mutation.text()}")
    before_stale_refusal = live_json(page, "GET", f"/api/proposals/{proposal_id}")
    page.get_by_role("button", name="Approve proposal").click()
    page.get_by_text("state_changed_since_proposal", exact=False).wait_for()
    stale = live_json(page, "GET", f"/api/proposals/{proposal_id}")
    require(stale["state_changed"] is True, "stale proposal was not detected by the API")
    require(stale["state"] == "awaiting_approval", "stale refusal unexpectedly decided proposal")
    require(
        page.get_by_role("button", name="Execute approved action").count() == 0,
        "stale proposal exposed an execution control",
    )
    after_stale_refusal = live_json(page, "GET", f"/api/proposals/{proposal_id}")
    for field in (
        "current_desired_replicas",
        "current_ready_replicas",
        "current_resource_version",
        "current_state_fingerprint",
    ):
        require(
            after_stale_refusal[field] == before_stale_refusal[field],
            f"stale refusal changed simulator state field {field}",
        )


class Diagnostics:
    def __init__(self, page: Page) -> None:
        self.console_errors: list[str] = []
        self.api_statuses: list[int] = []
        self.request_failures: list[str] = []

        def on_console(message: ConsoleMessage) -> None:
            if message.type == "error" and "Failed to load resource" not in message.text:
                self.console_errors.append(message.text)

        def on_response(response: Response) -> None:
            if "/api/" in response.url:
                self.api_statuses.append(response.status)

        def on_request_failed(request: Request) -> None:
            if "favicon.ico" not in request.url:
                self.request_failures.append(f"{request.url} {request.failure}")

        page.on("console", on_console)
        page.on("pageerror", lambda error: self.console_errors.append(str(error)))
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    def verify(self) -> None:
        require(
            not self.request_failures,
            f"browser requests failed: {'; '.join(self.request_failures)}",
        )
        require(
            not self.console_errors,
            f"browser console errors: {'; '.join(self.console_errors)}",
        )
        require(bool(self.api_statuses), "browser suite did not exercise API requests")
        expected = {200, 202, 304, 404, 409, 422, 503}
        unexpected = [status for status in self.api_statuses if status not in expected]
        require(not unexpected, f"unexpected API status codes: {unexpected}")


CHECKS: list[tuple[str, Callable[[Page], None]]] = [
    ("document shell and landmarks", check_document_shell),
    ("scenario run and approval", check_scenario_run_and_approval),
    ("incident timeline and evidence references", check_incident_timeline_and_evidence),
    ("unknown verification is not recovery", check_unknown_verification_is_not_recovery),
    ("Problem Details error state", check_problem_details),
    ("keyboard navigation", check_keyboard_navigation),
    ("not-found route", check_route_not_found),
    ("live approval, rejection, stale refusal, and reload", check_live_operator_flows),
]


def run(page: Page) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for name, check in CHECKS:
        reset_routes(page)
        try:
            check(page)
        except (CheckFailure, PlaywrightError) as error:
            failures.append((name, f"{type(error).__name__}: {error}"))
            print(f"FAIL  {name}: {error}")
        else:
            print(f"PASS  {name}")
    return failures


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        diagnostics = Diagnostics(page)
        try:
            failures = run(page)
        finally:
            reset_routes(page)
            browser.close()

    try:
        diagnostics.verify()
    except CheckFailure as error:
        failures.append(("browser diagnostics", str(error)))
        print(f"FAIL  browser diagnostics: {error}")
    else:
        print("PASS  browser diagnostics")

    total = len(CHECKS) + 1
    print(f"\n{total - len(failures)}/{total} browser checks passed")
    if failures:
        for name, message in failures:
            print(f"  - {name}: {message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
