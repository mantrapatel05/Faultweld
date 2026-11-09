import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { incidentDetail, problem, proposalDetail, renderAt, stubFetch } from "./support";

const DETAIL_PATH = "/api/incidents/inc-1";
const PROPOSAL_PATH = "/api/proposals/proposal-1";

describe("incident workspace route", () => {
  it("shows explicitly empty panels and issues no request when no incident is selected", () => {
    const fetchStub = stubFetch({});

    renderAt("/incidents");

    expect(screen.getByRole("heading", { level: 1, name: "No incident selected" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { name: "No incident selected" })).toHaveLength(2);
    expect(screen.getByRole("link", { name: "View incident index" })).toHaveAttribute("href", "/overview");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchStub.calls).toHaveLength(0);
  });

  it("shows a loading panel before the incident record resolves", () => {
    stubFetch({ [DETAIL_PATH]: { body: incidentDetail(), delayMs: 0 } });
    renderAt("/incidents/inc-1");

    expect(screen.getByRole("heading", { name: "Loading incident state" })).toBeInTheDocument();
    expect(screen.getByText("Loading", { exact: true })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders the authoritative audit timeline and evidence references", async () => {
    const detail = incidentDetail({
      proposal_ids: ["proposal-1"],
      timeline: [{
        event_id: 1,
        occurred_at: "2026-02-01T09:30:00Z",
        kind: "proposal_recorded",
        subject: "proposal-1",
        detail: { proposal_id: "proposal-1", policy_decision: "require_approval" }
      }]
    });
    const fetchStub = stubFetch({
      [DETAIL_PATH]: { body: detail },
      [PROPOSAL_PATH]: { body: proposalDetail() }
    });

    renderAt("/incidents/inc-1");

    expect(await screen.findByText("proposal recorded")).toBeInTheDocument();
    expect(screen.getByText("evidence-1")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Action proposal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve proposal" })).toBeInTheDocument();
    expect(fetchStub.calls).toContain("/api/proposals/proposal-1");
  });

  it("keeps evidence and proposal panels empty when there is no associated proposal", async () => {
    stubFetch({ [DETAIL_PATH]: { body: incidentDetail({ proposal_ids: [] }) } });
    renderAt("/incidents/inc-1");

    expect(await screen.findByRole("heading", { name: "No timeline events recorded" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "No evidence references loaded" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "No action proposal loaded" })).toBeInTheDocument();
  });


  it("distinguishes an unavailable proposal from an investigation with no proposal", async () => {
    stubFetch({
      [DETAIL_PATH]: { body: incidentDetail({ proposal_ids: ["proposal-1"] }) },
      [PROPOSAL_PATH]: {
        status: 503,
        contentType: "application/problem+json",
        body: problem({ detail: "Proposal storage is temporarily unavailable." })
      }
    });
    renderAt("/incidents/inc-1");

    expect(await screen.findByRole("heading", { name: "Proposal details unavailable" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Evidence references unavailable" })).toBeInTheDocument();
    expect(screen.getByText("Proposal storage is temporarily unavailable.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "No action proposal loaded" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve proposal" })).not.toBeInTheDocument();
  });

  it("does not offer approval when any returned precondition fails", async () => {
    stubFetch({
      [DETAIL_PATH]: { body: incidentDetail({ proposal_ids: ["proposal-1"] }) },
      [PROPOSAL_PATH]: {
        body: proposalDetail({
          preconditions: [{
            name: "target_identity",
            expected: "faultweld-demo/Deployment/faultweld-sample@uid-1",
            observed: "faultweld-demo/Deployment/other@uid-2",
            passed: false,
            checked_at: "2026-02-01T09:30:00Z"
          }]
        })
      }
    });
    renderAt("/incidents/inc-1");

    expect(await screen.findByText("Expected: faultweld-demo/Deployment/faultweld-sample@uid-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve proposal" })).not.toBeInTheDocument();
  });

  it("reports a not-found problem without inventing incident state", async () => {
    const details = problem({
      type: "urn:faultweld:problem:not-found",
      title: "Incident not found",
      status: 404,
      detail: "The requested incident does not exist.",
      instance: DETAIL_PATH,
      code: "incident_not_found"
    });
    stubFetch({ [DETAIL_PATH]: { status: 404, body: details, contentType: "application/problem+json" } });
    renderAt("/incidents/inc-1");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("heading", { name: "Incident state unavailable" })).toBeInTheDocument();
    expect(within(alert).getByText(details.detail)).toBeInTheDocument();
    expect(within(alert).getByRole("link", { name: "Return to overview" })).toHaveAttribute("href", "/overview");
    expect(screen.getByText("Disconnected", { exact: true })).toBeInTheDocument();
  });

  it("does not offer approval after the current state fingerprint changes", async () => {
    stubFetch({
      [DETAIL_PATH]: { body: incidentDetail({ proposal_ids: ["proposal-1"] }) },
      [PROPOSAL_PATH]: { body: proposalDetail({ state_changed: true, current_state_fingerprint: "c".repeat(64) }) }
    });
    renderAt("/incidents/inc-1");

    expect(await screen.findByRole("heading", { name: "Live state changed since proposal" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve proposal" })).not.toBeInTheDocument();
  });
});
