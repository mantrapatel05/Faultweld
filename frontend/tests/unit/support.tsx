import { render, type RenderResult } from "@testing-library/react";
import { vi } from "vitest";

import App from "../../src/App";
import type { IncidentStatus, IncidentSummary, ProblemDetails } from "../../src/api/types";

export interface StubRoute {
  status?: number;
  body: unknown;
  contentType?: string;
  delayMs?: number;
}

export type StubHandler = StubRoute | (() => Response);

export interface FetchStub {
  calls: string[];
  /** Releases any response that was registered with `delayMs`. */
  deferred: () => void;
}

export function incidentSummary(overrides: Partial<IncidentSummary> = {}): IncidentSummary {
  return {
    incident_id: "inc-1",
    scenario_id: "cache-pressure",
    status: "open",
    opened_at: "2026-02-01T09:00:00Z",
    updated_at: "2026-02-01T09:30:00Z",
    ...overrides
  };
}

export function problem(overrides: Partial<ProblemDetails> = {}): ProblemDetails {
  return {
    type: "urn:faultweld:problem:readiness",
    title: "Readiness check failed",
    status: 503,
    detail: "Persistence is not configured for this foundation build.",
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

/**
 * Installs a `fetch` stub that answers by longest matching path suffix, so that
 * `/api/incidents/inc-1` resolves before the `/api/incidents` index route.
 */
export function stubFetch(routes: Record<string, StubHandler>): FetchStub {
  const calls: string[] = [];
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });

  const entries = Object.entries(routes).sort(([left], [right]) => right.length - left.length);

  const fetchMock = vi.fn(async (input: unknown): Promise<Response> => {
    const url = String(input);
    calls.push(url);

    const matched = entries.find(([suffix]) => url.endsWith(suffix));
    if (matched === undefined) {
      return new Response(JSON.stringify(problem({ instance: url })), {
        status: 501,
        headers: { "Content-Type": "application/problem+json" }
      });
    }

    const route = matched[1];
    if (typeof route !== "function" && route.delayMs !== undefined) {
      await gate;
      await wait(route.delayMs);
    }

    return typeof route === "function" ? route() : jsonResponse(route);
  });

  vi.stubGlobal("fetch", fetchMock);

  return { calls, deferred: release };
}

/** Renders the real application shell at a specific client route. */
export function renderAt(path: string): RenderResult {
  window.history.pushState({}, "", path);
  return render(<App />);
}

export function statusLabel(status: IncidentStatus): string {
  return status.replaceAll("_", " ");
}
