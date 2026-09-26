import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import { ApiError, getIncident, listIncidents } from "../../src/api/client";
import { problem } from "./support";

const BASE_URL = "http://127.0.0.1:8000";

type FetchMock = Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>;

function stubFetchResponse(response: Response): FetchMock {
  const fetchMock: FetchMock = vi.fn(async (input, init) => {
    void input;
    void init;
    return response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function json(body: unknown, status = 200, contentType = "application/json"): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": contentType } });
}

function lastRequest(fetchMock: FetchMock): [string, RequestInit | undefined] {
  const call = fetchMock.mock.calls.at(-1);
  if (call === undefined) {
    throw new Error("fetch was never called");
  }
  return [String(call[0]), call[1]];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("requests the incident index with the documented Accept header", async () => {
    const fetchMock = stubFetchResponse(json({ items: [], next_cursor: null }));

    await expect(listIncidents()).resolves.toEqual({ items: [], next_cursor: null });

    const [url, init] = lastRequest(fetchMock);
    expect(url).toBe(`${BASE_URL}/api/incidents`);
    expect((init?.headers as Record<string, string>).Accept).toBe(
      "application/json, application/problem+json"
    );
  });

  it("encodes incident identifiers in detail requests", async () => {
    const fetchMock = stubFetchResponse(json({}));

    await getIncident("inc 1/2");

    expect(lastRequest(fetchMock)[0]).toBe(`${BASE_URL}/api/incidents/inc%201%2F2`);
  });

  it("raises an ApiError carrying the problem document and its status", async () => {
    const details = problem({ status: 503 });
    stubFetchResponse(json(details, 503, "application/problem+json"));

    const error = await listIncidents().catch((thrown: unknown) => thrown);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(503);
    expect(apiError.problem).toEqual(details);
    expect(apiError.message).toBe(details.detail);
  });

  it("rejects a problem document that is missing contract fields", async () => {
    stubFetchResponse(json({ ...problem(), code: undefined }, 500, "application/problem+json"));

    const error = await listIncidents().catch((thrown: unknown) => thrown);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.problem).toBeNull();
    expect(apiError.message).toBe("The API returned HTTP 500.");
  });

  it("falls back to a status message when the error body is not JSON", async () => {
    stubFetchResponse(new Response("<html>502</html>", { status: 502 }));

    const error = await listIncidents().catch((thrown: unknown) => thrown);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.problem).toBeNull();
    expect(apiError.message).toBe("The API returned HTTP 502.");
  });

  it("never issues a mutating request from the read-only client", async () => {
    const fetchMock = stubFetchResponse(json({ items: [], next_cursor: null }));

    await listIncidents();
    await getIncident("inc-1").catch(() => undefined);

    expect(fetchMock.mock.calls.length).toBe(2);
    for (const call of fetchMock.mock.calls) {
      expect(call[1]?.method ?? "GET").toBe("GET");
    }
  });
});
