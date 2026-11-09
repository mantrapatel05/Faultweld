import type {
  DecisionRequest,
  DecisionResponse,
  ExecuteResponse,
  IncidentDetailResponse,
  IncidentListResponse,
  ProblemDetails,
  ProposalDetailResponse,
  ScenarioRunRequest,
  ScenarioRunResponse
} from "./types";

// The Vite proxy keeps local browser requests same-origin. Production should
// route /api to the backend through its reverse proxy unless CORS is configured.
const apiBaseUrl = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetails | null;

  constructor(status: number, problem: ProblemDetails | null) {
    super(problem?.detail ?? `The API returned HTTP ${status}.`);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }
}

function isProblemDetails(value: unknown): value is ProblemDetails {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.type === "string" &&
    typeof record.title === "string" &&
    typeof record.status === "number" &&
    typeof record.detail === "string" &&
    typeof record.instance === "string" &&
    typeof record.code === "string"
  );
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json, application/problem+json");
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${apiBaseUrl}${path}`, { ...init, headers });
  if (!response.ok) {
    let problem: ProblemDetails | null = null;
    try {
      const body: unknown = await response.json();
      if (isProblemDetails(body)) {
        problem = body;
      }
    } catch {
      problem = null;
    }
    throw new ApiError(response.status, problem);
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(response.status, null);
  }
}

function encodeId(value: string): string {
  return encodeURIComponent(value);
}

export interface IncidentListOptions {
  limit?: number;
  cursor?: string | null;
}

export function listIncidents(options: IncidentListOptions = {}): Promise<IncidentListResponse> {
  const query = new URLSearchParams();
  if (options.limit !== undefined) {
    query.set("limit", String(options.limit));
  }
  if (options.cursor) {
    query.set("cursor", options.cursor);
  }
  const encodedQuery = query.toString();
  const suffix = encodedQuery ? `?${encodedQuery}` : "";
  return requestJson<IncidentListResponse>(`/api/incidents${suffix}`);
}

export function getIncident(incidentId: string): Promise<IncidentDetailResponse> {
  return requestJson<IncidentDetailResponse>(`/api/incidents/${encodeId(incidentId)}`);
}

export function getProposal(proposalId: string): Promise<ProposalDetailResponse> {
  return requestJson<ProposalDetailResponse>(`/api/proposals/${encodeId(proposalId)}`);
}

export function createScenarioRun(
  scenarioId: string,
  payload: ScenarioRunRequest = {}
): Promise<ScenarioRunResponse> {
  return requestJson<ScenarioRunResponse>(`/api/scenarios/${encodeId(scenarioId)}/runs`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function decideProposal(
  proposalId: string,
  payload: DecisionRequest
): Promise<DecisionResponse> {
  return requestJson<DecisionResponse>(`/api/proposals/${encodeId(proposalId)}/decision`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function executeProposal(proposalId: string): Promise<ExecuteResponse> {
  return requestJson<ExecuteResponse>(`/api/proposals/${encodeId(proposalId)}/execute`, {
    method: "POST",
    body: "{}"
  });
}
