export type IncidentStatus =
  | "open"
  | "investigating"
  | "action_proposed"
  | "awaiting_approval"
  | "executing"
  | "verifying"
  | "recovered"
  | "not_recovered"
  | "unknown"
  | "closed"
  | "escalated";

export interface IncidentSummary {
  incident_id: string;
  scenario_id: string;
  status: IncidentStatus;
  opened_at: string;
  updated_at: string;
}

export interface IncidentListResponse {
  items: IncidentSummary[];
  next_cursor: string | null;
}

export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string;
  code: string;
}
