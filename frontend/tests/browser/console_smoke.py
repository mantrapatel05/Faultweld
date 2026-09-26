"""Browser smoke and accessibility checks for the Faultweld incident console.

These checks run against the real Vite dev server and a real Chromium instance.
No model provider or Kubernetes cluster is required, and every API response is
stubbed at the network boundary so the assertions stay deterministic.

Two deliberate deviations from the full state matrix are documented here:

* The loading state is not asserted in this suite. A Playwright sync route
  handler cannot delay a response without blocking the dispatcher that would
  observe the intermediate state, so that state is asserted deterministically
  by the Vitest component suite instead.
* Chromium logs every non-2xx response as a ``Failed to load resource`` console
  error. The error-state checks provoke those responses on purpose, so those
  messages are filtered and replaced by a stricter assertion on the observed
  API status codes.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from itertools import pairwise
from typing import Any

from playwright.sync_api import ConsoleMessage, Page, Request, Response, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

BASE_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
API_BASE_URL = os.environ.get("VITE_API_URL", "http://127.0.0.1:8000").rstrip("/")

INDEX_ROUTE = re.compile(r"/api/incidents$")
DETAIL_ROUTE = re.compile(r"/api/incidents/[^/?#]+$")

IGNORED_REQUEST_FRAGMENTS = ("favicon.ico",)
IGNORED_CONSOLE_FRAGMENTS = ("Failed to load resource",)
EXPECTED_API_STATUSES = {200, 404, 503}

DISCONNECTED_NOTE = (
    "The workspace remains disconnected. No incident state is being inferred from the browser."
)
OUTLINE_EXPRESSION = (
    "element => getComputedStyle(element).outlineStyle"
    " + ' ' + getComputedStyle(element).outlineWidth"
)

INCIDENTS: list[dict[str, str]] = [
    {
        "incident_id": "inc-1",
        "scenario_id": "cache-pressure",
        "status": "open",
        "opened_at": "2026-02-01T09:00:00Z",
        "updated_at": "2026-02-01T09:30:00Z",
    },
    {
        "incident_id": "inc-2",
        "scenario_id": "bad-image",
        "status": "action_proposed",
        "opened_at": "2026-02-02T11:00:00Z",
        "updated_at": "2026-02-02T11:45:00Z",
    },
]

NOT_FOUND_PROBLEM: dict[str, Any] = {
    "type": "urn:faultweld:problem:not-found",
    "title": "Incident not found",
    "status": 404,
    "detail": "The requested incident does not exist in the local read-only store.",
    "instance": "/api/incidents/inc-1",
    "code": "incident_not_found",
}

UNAVAILABLE_PROBLEM: dict[str, Any] = {
    "type": "urn:faultweld:problem:readiness",
    "title": "Readiness check failed",
    "status": 503,
    "detail": "Persistence is not configured for this foundation build.",
    "instance": "/api/incidents",
    "code": "dependency_unavailable",
}


class CheckFailure(AssertionError):
    """Raised when a browser check does not hold."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def stub_json(route: Route, body: Any, status: int) -> None:
    media_type = "application/problem+json" if status >= 400 else "application/json"
    route.fulfill(status=status, content_type=media_type, body=json.dumps(body))


def clear_stubs(page: Page) -> None:
    page.unroute(INDEX_ROUTE)
    page.unroute(DETAIL_ROUTE)


def stub_index(page: Page, body: Any, status: int = 200) -> None:
    page.route(INDEX_ROUTE, lambda route: stub_json(route, body, status))


def stub_detail(page: Page, body: Any, status: int = 200) -> None:
    page.route(DETAIL_ROUTE, lambda route: stub_json(route, body, status))


def goto(page: Page, path: str) -> None:
    page.goto(f"{BASE_URL}{path}")
    page.wait_for_load_state("networkidle")


def heading_levels(page: Page) -> list[int]:
    levels: list[int] = page.eval_on_selector_all(
        "main h1, main h2, main h3, main h4, main h5, main h6",
        "elements => elements.map(element => Number(element.tagName.slice(1)))",
    )
    return levels


def check_landmarks(page: Page) -> None:
    require(page.get_by_role("banner").count() == 1, "expected exactly one banner landmark")
    require(
        page.get_by_role("navigation", name="Primary navigation").count() == 1,
        "expected exactly one labelled primary navigation landmark",
    )
    require(page.get_by_role("main").count() == 1, "expected exactly one main landmark")
    require(
        page.get_by_role("complementary", name="Faultweld navigation").count() == 1,
        "expected exactly one labelled complementary landmark",
    )
    require(
        page.get_by_role("contentinfo").count() == 1, "expected exactly one contentinfo landmark"
    )
    require(page.locator("main h1").count() == 1, "expected exactly one h1 inside main")


def check_heading_structure(page: Page) -> None:
    levels = heading_levels(page)
    require(levels[:1] == [1], f"main content must start with a single h1, found {levels}")
    for previous, current in pairwise(levels):
        require(current - previous <= 1, f"heading level skipped from h{previous} to h{current}")
    for text in page.locator("main :is(h1,h2,h3,h4,h5,h6)").all_text_contents():
        require(text.strip() != "", "a heading rendered without an accessible name")


def check_document_shell(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})

    goto(page, "/overview")
    page.get_by_role("heading", name="No incidents yet").wait_for()

    require(
        page.evaluate("document.documentElement.lang") == "en",
        "the document language must be declared for assistive technology",
    )
    require(page.title() == "Faultweld", f"unexpected document title: {page.title()!r}")
    check_landmarks(page)


def check_overview_empty_state(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})

    goto(page, "/overview")

    page.get_by_role("heading", name="No incidents yet").wait_for()
    require(
        page.get_by_text("00 records").count() == 1, "empty overview should report zero records"
    )
    require(
        page.get_by_role("alert").count() == 0, "a reachable empty API must not render an alert"
    )
    require(
        page.get_by_text("Local read-only", exact=True).count() == 1, "read-only badge is missing"
    )
    require(
        page.get_by_role("heading", name="Recent incidents").count() == 1,
        "the incident index heading is missing",
    )
    check_heading_structure(page)


def check_overview_ready_state(page: Page) -> None:
    stub_index(page, {"items": INCIDENTS, "next_cursor": None})

    goto(page, "/overview")

    page.get_by_role("list", name="Incidents").wait_for()
    rows = page.get_by_role("list", name="Incidents").get_by_role("listitem")
    require(rows.count() == 2, f"expected two incident rows, found {rows.count()}")
    require(page.get_by_text("02 records").count() == 1, "overview should report two records")
    require(
        rows.nth(0).get_by_role("link").get_attribute("href") == "/incidents/inc-1",
        "the first incident row must link to its workspace route",
    )
    require(
        rows.nth(1).get_by_text("action proposed", exact=True).count() == 1,
        "underscore status values must render as readable text",
    )
    timestamps = page.locator("main time")
    require(timestamps.count() == 2, "each incident row must render a machine-readable timestamp")
    require(
        timestamps.nth(0).get_attribute("datetime") == "2026-02-01T09:30:00Z",
        "the incident timestamp must be exposed verbatim",
    )
    check_heading_structure(page)


def check_overview_error_state(page: Page) -> None:
    stub_index(page, UNAVAILABLE_PROBLEM, status=503)

    goto(page, "/overview")

    alert = page.get_by_role("alert")
    alert.wait_for()
    require(alert.count() == 1, "an unavailable API must render exactly one alert region")
    require(
        alert.get_by_role("heading", name="The incident index is unavailable").count() == 1,
        "the unavailable alert must be titled",
    )
    require(
        alert.get_by_text(str(UNAVAILABLE_PROBLEM["detail"])).count() == 1,
        "the problem detail must be shown to the operator",
    )
    require(
        page.get_by_text("Disconnected", exact=True).count() == 1,
        "an error state must be labelled disconnected",
    )
    require(
        alert.get_by_text(DISCONNECTED_NOTE).count() == 1,
        "the disconnected note must be shown to the operator",
    )
    check_landmarks(page)


def check_workspace_empty_state(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})

    goto(page, "/incidents")

    page.locator("#workspace-title").wait_for()
    require(
        page.get_by_role("heading", name="No incident selected").count() == 2,
        "an unselected workspace must show an empty heading in the page and the timeline panel",
    )
    require(
        page.get_by_role("heading", name="No evidence loaded").count() == 1,
        "the evidence panel must stay explicitly empty",
    )
    require(
        page.get_by_role("heading", name="No action proposal loaded").count() == 1,
        "no action proposal may be shown before the API exposes one",
    )
    require(page.get_by_role("alert").count() == 0, "an unselected workspace is not an error")
    require(
        page.get_by_text("Read-only", exact=True).count() == 1,
        "the action panel must stay read-only",
    )
    check_landmarks(page)
    check_heading_structure(page)


def check_workspace_detail_state(page: Page) -> None:
    stub_detail(page, INCIDENTS[0])

    goto(page, "/incidents/inc-1")

    page.get_by_role("heading", name="Timeline events are not in this contract").wait_for()
    require(
        page.locator("#workspace-title").inner_text() == "inc-1",
        "the workspace must display the requested incident identifier",
    )
    require(
        page.get_by_role("heading", name="No evidence loaded").count() == 1,
        "the evidence panel must stay explicitly empty for a loaded incident",
    )
    require(page.get_by_role("alert").count() == 0, "a loaded incident is not an error")
    check_heading_structure(page)


def check_workspace_not_found_state(page: Page) -> None:
    stub_detail(page, NOT_FOUND_PROBLEM, status=404)

    goto(page, "/incidents/inc-1")

    alert = page.get_by_role("alert")
    alert.wait_for()
    require(
        alert.get_by_role("heading", name="Incident state unavailable").count() == 1,
        "a missing incident must render a titled error panel",
    )
    require(
        alert.get_by_text(str(NOT_FOUND_PROBLEM["detail"])).count() == 1,
        "the not-found problem detail must be shown to the operator",
    )
    require(
        alert.get_by_role("link", name="Return to overview").get_attribute("href") == "/overview",
        "the error panel must offer a route back to the overview",
    )


def check_not_found_route(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})

    goto(page, "/not-a-real-route")

    page.get_by_role("heading", name="That surface is not wired.").wait_for()
    require(
        page.get_by_role("link", name="Go to overview").get_attribute("href") == "/overview",
        "the not-found surface must link back to the overview",
    )
    check_landmarks(page)
    check_heading_structure(page)


def check_index_redirect(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})

    goto(page, "/")

    page.get_by_role("heading", name="Incident response, at a glance.").wait_for()
    require(
        page.url.rstrip("/").endswith("/overview"),
        f"the index route must redirect, landed on {page.url}",
    )


def check_keyboard_only_navigation(page: Page) -> None:
    stub_index(page, {"items": [], "next_cursor": None})

    goto(page, "/overview")
    page.get_by_role("heading", name="No incidents yet").wait_for()

    nav = page.get_by_role("navigation", name="Primary navigation")
    overview = nav.get_by_role("link", name="Overview")
    workspace = nav.get_by_role("link", name="Incident workspace")

    page.get_by_role("link", name="Faultweld overview").focus()
    require(
        page.locator(":focus").get_attribute("href") == "/overview",
        "the brand link must be focusable before the navigation links",
    )

    page.keyboard.press("Tab")
    require(
        page.locator(":focus").get_attribute("href") == "/overview",
        "tabbing once from the brand link must reach the overview link",
    )

    outline = page.eval_on_selector(":focus", OUTLINE_EXPRESSION)
    require(
        "none" not in outline, f"keyboard focus must be visible, computed outline was {outline!r}"
    )

    page.keyboard.press("Tab")
    require(
        page.locator(":focus").get_attribute("href") == "/incidents",
        "tabbing again must reach the incident workspace link",
    )

    page.keyboard.press("Enter")
    page.locator("#workspace-title").wait_for()
    require(
        page.url.rstrip("/").endswith("/incidents"),
        "Enter must activate the focused navigation link",
    )
    require(overview.get_attribute("aria-current") is None, "the overview link must not be current")
    require(
        workspace.get_attribute("aria-current") == "page",
        "the active link must expose aria-current=page",
    )


def check_no_unverified_recovery_claims(page: Page) -> None:
    stub_index(
        page,
        {
            "items": [{**INCIDENTS[0], "incident_id": "inc-9", "status": "recovered"}],
            "next_cursor": None,
        },
    )

    goto(page, "/overview")
    page.get_by_role("list", name="Incidents").wait_for()

    for heading in page.locator("main :is(h1,h2,h3,h4,h5,h6)").all_text_contents():
        for forbidden in ("recovered", "resolved", "fixed"):
            require(
                forbidden not in heading.lower(),
                f"the console must not present {forbidden!r} as a verified outcome",
            )


class Diagnostics:
    def __init__(self, page: Page) -> None:
        self.console_errors: list[str] = []
        self.api_statuses: list[int] = []
        self.request_failures: list[str] = []

        def on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                self.console_errors.append(f"console.error: {message.text}")

        def on_response(response: Response) -> None:
            if response.url.startswith(f"{API_BASE_URL}/api/"):
                self.api_statuses.append(response.status)

        def on_request_failed(request: Request) -> None:
            if not any(fragment in request.url for fragment in IGNORED_REQUEST_FRAGMENTS):
                self.request_failures.append(f"{request.url} {request.failure}")

        page.on("console", on_console)
        page.on("pageerror", lambda error: self.console_errors.append(f"pageerror: {error}"))
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    def verify(self) -> None:
        require(
            not self.request_failures,
            f"requests failed at the network layer: {'; '.join(self.request_failures)}",
        )
        unexpected = [
            error
            for error in self.console_errors
            if not any(fragment in error for fragment in IGNORED_CONSOLE_FRAGMENTS)
        ]
        require(not unexpected, f"unexpected browser console errors: {'; '.join(unexpected)}")
        require(bool(self.api_statuses), "the suite never exercised the read-only API client")
        unexpected_statuses = [
            status for status in self.api_statuses if status not in EXPECTED_API_STATUSES
        ]
        require(
            not unexpected_statuses,
            f"the API returned unanticipated statuses: {sorted(set(unexpected_statuses))}",
        )


CHECKS: list[tuple[str, Callable[[Page], None]]] = [
    ("document shell and landmarks", check_document_shell),
    ("overview empty state", check_overview_empty_state),
    ("overview ready state", check_overview_ready_state),
    ("overview error state", check_overview_error_state),
    ("workspace empty state", check_workspace_empty_state),
    ("workspace detail state", check_workspace_detail_state),
    ("workspace not-found state", check_workspace_not_found_state),
    ("not-found route", check_not_found_route),
    ("index redirect", check_index_redirect),
    ("keyboard-only navigation", check_keyboard_only_navigation),
    ("no unverified recovery claims", check_no_unverified_recovery_claims),
]


def run(page: Page) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for name, check in CHECKS:
        clear_stubs(page)
        try:
            check(page)
        except CheckFailure as error:
            failures.append((name, str(error)))
            print(f"FAIL  {name}: {error}")
        except PlaywrightError as error:
            failures.append((name, f"{type(error).__name__}: {error}"))
            print(f"ERROR {name}: {type(error).__name__}: {error}")
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
            clear_stubs(page)
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
        print("\nFailed checks:")
        for name, message in failures:
            print(f"  - {name}: {message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
