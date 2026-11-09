import { render, type RenderResult } from "@testing-library/react";
import { vi } from "vitest";

import App from "../../src/App";
import type {
  IncidentDetailResponse,
  IncidentStatus,
  IncidentSummary,
  ProblemDetails,
  ProposalDetailResponse
} from "../../src/api/types";

export interface StubRoute {
  status?: number;
  body: unknown;
  contentType?: string;
  delayMs?: number;
}

export type StubHandler = StubRoute | ((input: string, init: RequestInit) => Response);

export interface FetchStub {
  calls: string[];
  deferred: () => void;
}

export function incidentSummary(overrides: Partial<IncidentSummary> = {}): IncidentSummary {
  return {
    incident_id: "inc-1",
    scenario_id: "healthy-replica-loss",
    status: "open",
    opened_at: "2026-02-01T09:00:00Z",
    updated_at: "2026-02-01T09:30:00Z",
    ...overrides
  };
}

export function incidentDetail(overrides: Partial<IncidentDetailResponse> = {}): IncidentDetailResponse {
  return {
    ...incidentSummary(),
    environment_id: "faultweld-lite",
    current_run_id: "run-1",
    terminal_reason: null,
    verification_status: null,
    proposal_ids: [],
    timeline: [],
    ...overrides
  };
}

export function proposalDetail(overrides: Partial<ProposalDetailResponse> = {}): ProposalDetailResponse {
  return {
    proposal_id: "proposal-1",
    incident_id: "inc-1",
    run_id: "run-1",
    version: 2,
    state: "awaiting_approval",
    action_type: "scale_deployment",
    target_cluster_id: "faultweld",
    target_namespace: "faultweld-demo",
    target_kind: "Deployment",
    target_name: "faultweld-sample",
    target_uid: "uid-1",
    proposed_replicas: 1,
    proposed_revision: null,
    current_desired_replicas: 2,
    current_available_replicas: 1,
    current_ready_replicas: 1,
    current_revision: 1,
    current_resource_version: 7,
    current_readiness_path: "/",
    current_readiness_port: "8080",
    expected_state_fingerprint: "a".repeat(64),
    current_state_fingerprint: "a".repeat(64),
    state_observed_at: "2026-02-01T09:29:00Z",
    current_state_observed_at: "2026-02-01T09:30:00Z",
    state_changed: false,
    preconditions: [
      {
        name: "live_state_fingerprint",
        expected: "a".repeat(64),
        observed: "a".repeat(64),
        passed: true,
        checked_at: "2026-02-01T09:30:00Z"
      }
    ],
    expected_postconditions: ["ready replicas equal desired replicas"],
    evidence_ids: ["evidence-1"],
    policy_decision: "require_approval",
    policy_reason_codes: ["approval_required"],
    policy_version: "1",
    policy_bundle_digest: "b".repeat(64),
    expires_at: "2099-02-01T09:30:00Z",
    ...overrides
  };
}

export function problem(overrides: Partial<ProblemDetails> = {}): ProblemDetails {
  return {
    type: "urn:faultweld:problem:readiness",
    title: "Readiness check failed",
    status: 503,
    detail: "The local API is unavailable.",
    instance: "/api/incidents",
    code: "dependency_unavailable",
    ...overrides
  };
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, milliseconds);
  });
}

function jsonResponse(route: StubRoute): Response {
  return new Response(JSON.stringify(route.body), {
    status: route.status ?? 200,
    headers: { "Content-Type": route.contentType ?? "application/json" }
  });
}

/** Installs a route-aware fetch stub; query strings do not affect path matching. */
export function stubFetch(routes: Record<string, StubHandler>): FetchStub {
  const calls: string[] = [];
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const entries = Object.entries(routes).sort(([left], [right]) => right.length - left.length);

  const fetchMock = vi.fn(async (input: unknown, init: RequestInit = {}): Promise<Response> => {
    const url = String(input);
    calls.push(url);
    const path = new URL(url, "http://faultweld.test").pathname;
    const matched = entries.find(([suffix]) => path.endsWith(suffix));
    if (matched === undefined) {
      return jsonResponse({
        status: 501,
        contentType: "application/problem+json",
        body: problem({ status: 501, title: "Unstubbed test route", detail: `No test route for ${path}`, instance: path, code: "test_route_missing" })
      });
    }

    const route = matched[1];
    if (typeof route !== "function" && route.delayMs !== undefined) {
      await gate;
      await wait(route.delayMs);
    }
    return typeof route === "function" ? route(url, init) : jsonResponse(route);
  });

  vi.stubGlobal("fetch", fetchMock);
  return { calls, deferred: release };
}

export function renderAt(path: string): RenderResult {
  window.history.pushState({}, "", path);
  return render(<App />);
}

export function statusLabel(status: IncidentStatus): string {
  return status.replaceAll("_", " ");
}
