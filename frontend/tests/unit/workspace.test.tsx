import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { incidentSummary, problem, renderAt, stubFetch } from "./support";

const DETAIL_PATH = "/api/incidents/inc-1";

describe("incident workspace route", () => {
  it("shows explicitly empty panels and issues no request when no incident is selected", () => {
    const fetchStub = stubFetch({});

    renderAt("/incidents");

    expect(screen.getByRole("heading", { level: 1, name: "No incident selected" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { name: "No incident selected" })).toHaveLength(2);
    expect(screen.getByRole("heading", { name: "No evidence loaded" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "No action proposal loaded" })).toBeInTheDocument();
    expect(screen.getByText("No record loaded")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View incident index" })).toHaveAttribute("href", "/overview");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchStub.calls).toHaveLength(0);
  });

  it("shows a loading panel before the incident record resolves", () => {
    stubFetch({ [DETAIL_PATH]: { body: incidentSummary(), delayMs: 0 } });

    renderAt("/incidents/inc-1");

    expect(screen.getByRole("heading", { name: "Loading incident state" })).toBeInTheDocument();
    expect(screen.getByText("Loading", { exact: true })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("states that the contract has no event history once the summary loads", async () => {
    const fetchStub = stubFetch({ [DETAIL_PATH]: { body: incidentSummary() } });

    renderAt("/incidents/inc-1");

    expect(
      await screen.findByRole("heading", { name: "Timeline events are not in this contract" })
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })[0]).toHaveTextContent("inc-1");
    expect(screen.getAllByText("inc-1").length).toBeGreaterThan(0);
    expect(fetchStub.calls[0]).toBe("http://127.0.0.1:8000/api/incidents/inc-1");
  });

  it("reports a not-found problem without inventing incident state", async () => {
    const details = problem({
      type: "urn:faultweld:problem:not-found",
      title: "Incident not found",
      status: 404,
      detail: "The requested incident does not exist in the local read-only store.",
      instance: DETAIL_PATH,
      code: "incident_not_found"
    });
    stubFetch({
      [DETAIL_PATH]: { status: 404, body: details, contentType: "application/problem+json" }
    });

    renderAt("/incidents/inc-1");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("heading", { name: "Incident state unavailable" })).toBeInTheDocument();
    expect(within(alert).getByText(details.detail)).toBeInTheDocument();
    expect(within(alert).getByRole("link", { name: "Return to overview" })).toHaveAttribute(
      "href",
      "/overview"
    );
    expect(screen.getByText("Disconnected", { exact: true })).toBeInTheDocument();
  });

  it("falls back to a safe message when the failure body is not JSON", async () => {
    stubFetch({
      [DETAIL_PATH]: () =>
        new Response("<html>gateway timeout</html>", {
          status: 504,
          headers: { "Content-Type": "text/html" }
        })
    });

    renderAt("/incidents/inc-1");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("The API returned HTTP 504.")).toBeInTheDocument();
  });

  it("keeps evidence and action panels empty for every workspace state", async () => {
    stubFetch({ [DETAIL_PATH]: { body: incidentSummary({ status: "awaiting_approval" }) } });

    renderAt("/incidents/inc-1");

    expect(await screen.findByRole("heading", { name: "No evidence loaded" })).toBeInTheDocument();
    const actionPanel = screen.getByRole("heading", { name: "No action proposal loaded" }).closest("article");
    expect(actionPanel).not.toBeNull();
    expect(within(actionPanel as HTMLElement).getByText("Read-only")).toBeInTheDocument();
  });
});
