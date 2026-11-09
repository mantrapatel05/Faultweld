import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createScenarioRun,
  decideProposal,
  executeProposal,
  getIncident,
  getProposal,
  listIncidents
} from "../../src/api/client";

function response(body: unknown, status = 200, contentType = "application/json"): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": contentType } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API client", () => {
  it("encodes list cursors as query parameters and requests JSON", async () => {
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (...args) => { void args; return response({ items: [], next_cursor: null }); }
    );
    vi.stubGlobal("fetch", fetchMock);

    await listIncidents({ limit: 10, cursor: "opaque + cursor" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/incidents?limit=10&cursor=opaque+%2B+cursor");
    expect(new Headers(init.headers).get("Accept")).toContain("application/problem+json");
  });

  it("encodes incident and proposal identifiers", async () => {
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (...args) => { void args; return response({}); }
    );
    vi.stubGlobal("fetch", fetchMock);

    await getIncident("inc 1/2");
    await getProposal("proposal 1/2");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/incidents/inc%201%2F2");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/proposals/proposal%201%2F2");
  });

  it("posts scenario scripts and versioned human decisions", async () => {
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (...args) => { void args; return response({}); }
    );
    vi.stubGlobal("fetch", fetchMock);

    await createScenarioRun("healthy-replica-loss", { script: "auto" });
    await decideProposal("proposal-1", { decision: "approve", expected_version: 2 });

    const scenarioInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const decisionInit = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(scenarioInit.method).toBe("POST");
    expect(JSON.parse(String(scenarioInit.body))).toEqual({ script: "auto" });
    expect(JSON.parse(String(decisionInit.body))).toEqual({ decision: "approve", expected_version: 2 });
  });

  it("treats HTTP 202 execution-unknown as a successful typed response", async () => {
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (...args) => { void args; return response({ reason_code: "execution_unknown" }, 202); }
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await executeProposal("proposal-1");

    expect(result.reason_code).toBe("execution_unknown");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: "POST", body: "{}" });
  });

  it("preserves structured Problem Details for API failures", async () => {
    const body = {
      type: "urn:faultweld:problem:conflict",
      title: "Operation conflict",
      status: 409,
      detail: "The proposal version is stale.",
      instance: "/api/proposals/proposal-1/decision",
      code: "stale_version"
    };
    vi.stubGlobal("fetch", vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (...args) => { void args; return response(body, 409, "application/problem+json"); }
    ));

    await expect(decideProposal("proposal-1", { decision: "approve", expected_version: 2 }))
      .rejects.toMatchObject({ status: 409, problem: body, message: body.detail });
  });

  it("falls back to a stable message for a non-JSON error response", async () => {
    vi.stubGlobal("fetch", vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (...args) => { void args; return new Response("gateway error", { status: 502 }); }
    ));

    await expect(listIncidents()).rejects.toThrow("The API returned HTTP 502.");
  });
});
