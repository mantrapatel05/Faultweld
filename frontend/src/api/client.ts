import type { IncidentListResponse, IncidentSummary, ProblemDetails } from "./types";

const apiBaseUrl = (import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

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

async function requestJson<T>(path: string): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    headers: { Accept: "application/json, application/problem+json" }
  });

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

  return (await response.json()) as T;
}

export function listIncidents(): Promise<IncidentListResponse> {
  return requestJson<IncidentListResponse>("/api/incidents");
}

export function getIncident(incidentId: string): Promise<IncidentSummary> {
  return requestJson<IncidentSummary>(`/api/incidents/${encodeURIComponent(incidentId)}`);
}
