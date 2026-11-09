import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { DecisionResponse, ExecuteResponse, ScenarioRunResponse } from "../../src/api/types";
import { incidentDetail, problem, proposalDetail, renderAt, stubFetch } from "./support";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const runResult: ScenarioRunResponse = {
  scenario_id: "healthy-replica-loss",
  run_id: "run-1",
  incident_id: "inc-1",
  incident_status: "awaiting_approval",
  investigation_status: "proposed",
  escalation_reason: null,
  proposal_id: "proposal-1",
  proposal_version: 2,
  policy_decision: "require_approval",
  policy_reason_codes: ["approval_required"],
  model_calls: 2,
  tokens_used: 120
};

const decisionResult: DecisionResponse = {
  proposal_id: "proposal-1",
  approval_id: "approval-1",
  decision: "approve",
  approver_principal_id: "local-demo-user",
  approver_role: "reviewer",
  expires_at: "2099-02-01T09:30:00Z",
  proposal_version: 3,
  already_decided: false
};

describe("operator workflow", () => {
  it("runs a scenario, records approval, executes through the gateway, and shows verifier recovery", async () => {
    const user = userEvent.setup();
    let currentProposal = proposalDetail();
    let currentIncident = incidentDetail({ proposal_ids: ["proposal-1"], status: "awaiting_approval" });
    const fetchStub = stubFetch({
      "/api/scenarios/healthy-replica-loss/runs": () => json(runResult),
      "/api/incidents/inc-1": () => json(currentIncident),
      "/api/proposals/proposal-1": () => json(currentProposal),
      "/api/proposals/proposal-1/decision": () => {
        currentProposal = proposalDetail({ state: "authorized", version: 3 });
        return json(decisionResult);
      },
      "/api/proposals/proposal-1/execute": () => {
        currentProposal = proposalDetail({ state: "recovered", version: 5 });
        currentIncident = incidentDetail({ proposal_ids: ["proposal-1"], status: "closed", verification_status: "recovered" });
        const executeResult: ExecuteResponse = {
          proposal_id: "proposal-1",
          reason_code: "executed",
          executed: true,
          attempt_id: "attempt-1",
          attempt_outcome: "executed",
          proposal_state: "recovered",
          verification_status: "recovered",
          writer_operations: 1
        };
        return json(executeResult);
      }
    });

    renderAt("/overview");
    await user.click(await screen.findByRole("button", { name: "Run scenario" }));
    expect(await screen.findByRole("heading", { level: 1, name: "inc-1" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Approve proposal" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approve proposal" }));
    expect(await screen.findByRole("button", { name: "Execute approved action" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Execute approved action" }));
    expect(await screen.findByRole("heading", { name: "Verifier confirmed recovery" })).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "independent verification confirmed recovery"
    );
    expect(screen.getAllByText("recovered", { exact: true }).length).toBeGreaterThan(0);
    expect(fetchStub.calls).toContain("/api/scenarios/healthy-replica-loss/runs");
    expect(fetchStub.calls).toContain("/api/proposals/proposal-1/decision");
    expect(fetchStub.calls).toContain("/api/proposals/proposal-1/execute");
    await waitFor(() => expect(screen.getByText("closed", { exact: true })).toBeInTheDocument());
  }, 10000);

  it("requires a rejection reason and never exposes an execute action after rejection", async () => {
    const user = userEvent.setup();
    let currentProposal = proposalDetail();
    let rejectionPayload: unknown;
    const fetchStub = stubFetch({
      "/api/incidents/inc-1": () => json(incidentDetail({ proposal_ids: ["proposal-1"], status: currentProposal.state === "denied" ? "escalated" : "awaiting_approval" })),
      "/api/proposals/proposal-1": () => json(currentProposal),
      "/api/proposals/proposal-1/decision": (_input, init) => {
        currentProposal = proposalDetail({ state: "denied", version: 3 });
        rejectionPayload = JSON.parse(String(init.body));
        return json({ ...decisionResult, decision: "reject" });
      }
    });

    renderAt("/incidents/inc-1");
    const rejectButton = await screen.findByRole("button", { name: "Reject proposal" });
    expect(rejectButton).toBeDisabled();
    await user.type(screen.getByLabelText("Rejection reason"), "Risk is not acceptable");
    expect(rejectButton).toBeEnabled();
    await user.click(rejectButton);

    expect(await screen.findByRole("heading", { name: "Policy denied this proposal" })).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent("Proposal rejected");
    expect(screen.queryByRole("button", { name: "Execute approved action" })).not.toBeInTheDocument();
    await waitFor(() => expect(fetchStub.calls).toContain("/api/proposals/proposal-1/decision"));
    expect(rejectionPayload).toEqual({ decision: "reject", expected_version: 2, reason: "Risk is not acceptable" });
  });

  it("supports keyboard-only approval", async () => {
    const user = userEvent.setup();
    let currentProposal = proposalDetail();
    stubFetch({
      "/api/incidents/inc-1": () => json(incidentDetail({ proposal_ids: ["proposal-1"] })),
      "/api/proposals/proposal-1": () => json(currentProposal),
      "/api/proposals/proposal-1/decision": () => {
        currentProposal = proposalDetail({ state: "authorized", version: 3 });
        return json(decisionResult);
      }
    });

    renderAt("/incidents/inc-1");
    const approve = await screen.findByRole("button", { name: "Approve proposal" });
    for (let tab = 0; tab < 30 && document.activeElement !== approve; tab += 1) {
      await user.tab();
    }
    expect(document.activeElement).toBe(approve);
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("button", { name: "Execute approved action" })).toBeInTheDocument();
  });

  it("shows the stable code when the server refuses a stale approval", async () => {
    const user = userEvent.setup();
    stubFetch({
      "/api/incidents/inc-1": { body: incidentDetail({ proposal_ids: ["proposal-1"] }) },
      "/api/proposals/proposal-1": { body: proposalDetail() },
      "/api/proposals/proposal-1/decision": {
        status: 409,
        contentType: "application/problem+json",
        body: problem({
          status: 409,
          title: "Operation conflict",
          detail: "The live state changed after proposal creation.",
          code: "state_changed_since_proposal"
        })
      }
    });

    renderAt("/incidents/inc-1");
    await user.click(await screen.findByRole("button", { name: "Approve proposal" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("state_changed_since_proposal");
    expect(screen.queryByRole("button", { name: "Execute approved action" })).not.toBeInTheDocument();
  });

  it("keeps an unknown execution verification visibly unknown", async () => {
    const user = userEvent.setup();
    const unknownResult: ExecuteResponse = {
      proposal_id: "proposal-1",
      reason_code: "execution_unknown",
      executed: true,
      attempt_id: "attempt-1",
      attempt_outcome: "executed",
      proposal_state: "escalated",
      verification_status: "unknown",
      writer_operations: 1
    };
    const currentProposal = proposalDetail({ state: "authorized", version: 3 });
    const fetchStub = stubFetch({
      "/api/incidents/inc-1": { body: incidentDetail({ proposal_ids: ["proposal-1"], status: "awaiting_approval" }) },
      "/api/proposals/proposal-1": { body: currentProposal },
      "/api/proposals/proposal-1/execute": { body: unknownResult, status: 202 }
    });

    renderAt("/incidents/inc-1");
    await user.click(await screen.findByRole("button", { name: "Execute approved action" }));
    expect(await screen.findByRole("heading", { name: "Verification outcome is unknown" })).toBeInTheDocument();
    expect(screen.getByText("unknown", { exact: true })).toBeInTheDocument();
    expect(screen.queryByText("Verifier confirmed recovery")).not.toBeInTheDocument();
    expect(fetchStub.calls).toContain("/api/proposals/proposal-1/execute");
    expect(screen.getByText("unknown", { exact: true })).not.toHaveClass("status-success");
  });

  it("does not style an unverified failed outcome as success", async () => {
    const user = userEvent.setup();
    let currentProposal = proposalDetail({ state: "authorized", version: 3 });
    const currentIncident = incidentDetail({ proposal_ids: ["proposal-1"] });
    const fetchStub = stubFetch({
      "/api/incidents/inc-1": { body: currentIncident },
      "/api/proposals/proposal-1": () => json(currentProposal),
      "/api/proposals/proposal-1/execute": () => {
        currentProposal = proposalDetail({ state: "not_recovered", version: 5 });
        return json({
          proposal_id: "proposal-1",
          reason_code: "executed",
          executed: true,
          attempt_id: "attempt-1",
          attempt_outcome: "executed",
          proposal_state: "not_recovered",
          verification_status: "not_recovered",
          writer_operations: 1
        } satisfies ExecuteResponse);
      }
    });

    renderAt("/incidents/inc-1");
    await user.click(await screen.findByRole("button", { name: "Execute approved action" }));
    const outcome = (await screen.findAllByText("not recovered", { exact: true }))[0];
    expect(outcome).toBeDefined();
    if (!outcome) throw new Error("verification status is missing");
    expect(outcome).toHaveClass("status-error");
    expect(outcome).not.toHaveClass("status-success");
    expect(screen.queryByRole("heading", { name: "Verifier confirmed recovery" })).not.toBeInTheDocument();
    expect(fetchStub.calls).toContain("/api/proposals/proposal-1/execute");
  });

  it("shows server problem details when the scenario API refuses a run", async () => {
    const user = userEvent.setup();
    stubFetch({
      "/api/scenarios/healthy-replica-loss/runs": {
        status: 404,
        contentType: "application/problem+json",
        body: problem({ status: 404, title: "Scenario not found", detail: "This scenario is not registered.", code: "scenario_not_found" })
      }
    });

    renderAt("/overview");
    await user.click(await screen.findByRole("button", { name: "Run scenario" }));
    expect(await screen.findByText("This scenario is not registered.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Incident console" })).toBeInTheDocument();
  });
});
