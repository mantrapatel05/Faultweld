import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  BrowserRouter,
  Link,
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
  useNavigate,
  useParams
} from "react-router-dom";

import {
  ApiError,
  createScenarioRun,
  decideProposal,
  executeProposal,
  getIncident,
  getProposal,
  listIncidents
} from "./api/client";
import type {
  ExecuteResponse,
  IncidentDetailResponse,
  IncidentSummary,
  ProposalDetailResponse,
  ScenarioRunRequest,
  TimelineEvent
} from "./api/types";

type RequestState = "loading" | "ready" | "empty" | "error";
type StatusTone = "neutral" | "success" | "warning" | "error";
type ScriptChoice = "auto" | "scale" | "restart" | "none";
type ActionKind = "approve" | "reject" | "execute";

type StatusBadgeProps = {
  children: ReactNode;
  tone?: StatusTone;
};

function StatusBadge({ children, tone = "neutral" }: StatusBadgeProps) {
  return <span className={`status-badge status-${tone}`}>{children}</span>;
}

type StatePanelProps = {
  title: string;
  children: ReactNode;
  tone?: StatusTone;
};

function StatePanel({ title, children, tone = "neutral" }: StatePanelProps) {
  return (
    <div className={`state-panel state-${tone}`} role={tone === "error" ? "alert" : undefined}>
      <div className="state-panel-heading">
        <span className="state-mark" aria-hidden="true" />
        <h2>{title}</h2>
      </div>
      <div className="state-panel-body">{children}</div>
    </div>
  );
}

const SCENARIOS = [
  {
    id: "healthy-replica-loss",
    label: "Healthy replica loss",
    description: "A replica disappears while the deployment remains a valid remediation target."
  },
  {
    id: "deployment-unavailable",
    label: "Deployment unavailable",
    description: "The service is unavailable and the scripted investigator may escalate without proposing an action."
  },
  {
    id: "readiness-regression",
    label: "Readiness regression",
    description: "A remediation can execute while post-action verification still fails."
  }
] as const;

const SCRIPT_CHOICES: { value: ScriptChoice; label: string }[] = [
  { value: "auto", label: "Scenario default" },
  { value: "scale", label: "Propose scale" },
  { value: "restart", label: "Propose restart" },
  { value: "none", label: "Do not propose an action" }
];

function formatStatus(status: string): string {
  return status.replaceAll("_", " ");
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(date);
}

function statusTone(status: string): StatusTone {
  if (status === "recovered" || status === "closed") {
    return "success";
  }
  if (status === "not_recovered" || status === "failed") {
    return "error";
  }
  if (status === "unknown" || status === "execution_unknown" || status === "escalated") {
    return "warning";
  }
  return "neutral";
}

function verificationTone(status: string | null): StatusTone {
  if (status === "recovered") return "success";
  if (status === "not_recovered") return "error";
  if (status === "unknown") return "warning";
  return "neutral";
}

function AppLayout() {
  return (
    <div className="app-frame">
      <aside className="side-rail" aria-label="Faultweld navigation">
        <NavLink className="brand-lockup" to="/overview" aria-label="Faultweld overview">
          <svg className="brand-mark" aria-hidden="true" viewBox="0 0 24 24">
            <path d="M6 19V5h12M6 10h9M6 15h6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="square" />
          </svg>
          <span className="brand-copy">
            <strong>Faultweld</strong>
            <small>Operator console</small>
          </span>
        </NavLink>
        <nav className="primary-nav" aria-label="Primary navigation">
          <NavLink className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`} end to="/overview">
            Overview
          </NavLink>
          <NavLink className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`} to="/incidents">
            Incident workspace
          </NavLink>
        </nav>
        <div className="rail-footer">
          <span className="pulse-dot" aria-hidden="true" />
          <span>
            Simulator only
            <small>Human approval required</small>
          </span>
        </div>
      </aside>
      <div className="main-frame">
        <header className="topbar">
          <div className="breadcrumb" aria-label="Breadcrumb">
            <span>Faultweld Lite</span>
            <span aria-hidden="true">/</span>
            <strong>Operations</strong>
          </div>
        </header>
        <main id="main-content" className="page-content">
          <Outlet />
        </main>
        <footer className="app-footer">
          <span>Faultweld / local incident console</span>
        </footer>
      </div>
    </div>
  );
}

function ScenarioLauncher() {
  const navigate = useNavigate();
  const [scenarioId, setScenarioId] = useState<string>(SCENARIOS[0].id);
  const [script, setScript] = useState<ScriptChoice>("auto");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  async function handleRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setRunning(true);
    setError("");
    try {
      const payload: ScenarioRunRequest = { script };
      const result = await createScenarioRun(scenarioId, payload);
      navigate(`/incidents/${encodeURIComponent(result.incident_id)}`);
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : "The scenario run could not be created.");
    } finally {
      setRunning(false);
    }
  }

  const selectedScenarioDescription = SCENARIOS.find((scenario) => scenario.id === scenarioId)?.description ?? "";

  return (
    <section className="scenario-launcher ledger-panel" aria-labelledby="scenario-launcher-title">
      <div className="panel-heading">
        <div>
          <h2 id="scenario-launcher-title">Start a scenario run</h2>
        </div>
        <StatusBadge>Simulator</StatusBadge>
      </div>
      <p className="scenario-description">Create an incident and inspect its proposal before you approve or execute anything.</p>
      <form className="scenario-form" onSubmit={(event) => void handleRun(event)}>
        <label className="field">
          <span>Scenario</span>
          <select value={scenarioId} onChange={(event) => setScenarioId(event.target.value)}>
            {SCENARIOS.map((scenario) => (
              <option value={scenario.id} key={scenario.id}>{scenario.label}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Investigation script</span>
          <select value={script} onChange={(event) => setScript(event.target.value as ScriptChoice)}>
            {SCRIPT_CHOICES.map((choice) => (
              <option value={choice.value} key={choice.value}>{choice.label}</option>
            ))}
          </select>
        </label>
        <div className="scenario-form-footer">
          <p className="field-hint">{selectedScenarioDescription}</p>
          <button className="button button-primary" type="submit" disabled={running}>
            {running ? "Starting run…" : "Run scenario"}
          </button>
        </div>
      </form>
      {error && <p className="inline-error" role="alert">{error}</p>}
    </section>
  );
}

function AuthorityPanel() {
  return (
    <aside className="authority-panel" aria-labelledby="authority-title">
      <h2 id="authority-title">Nothing executes by default.</h2>
      <p className="authority-copy">
        Each step is enforced by the backend, not by the browser. The console only renders the
        records the API returns.
      </p>
      <ol className="authority-list">
        <li>
          <span className="authority-index" aria-hidden="true">01</span>
          <div>
            <strong>Investigate</strong>
            <p>The scripted investigator reads a simulated snapshot and records evidence references.</p>
          </div>
        </li>
        <li>
          <span className="authority-index" aria-hidden="true">02</span>
          <div>
            <strong>Decide</strong>
            <p>Policy evaluates the proposal. A human approval is recorded separately from execution.</p>
          </div>
        </li>
        <li>
          <span className="authority-index" aria-hidden="true">03</span>
          <div>
            <strong>Execute and verify</strong>
            <p>The gateway re-checks preconditions, the simulator applies the action, and an independent verifier reports the outcome.</p>
          </div>
        </li>
      </ol>
    </aside>
  );
}

function IncidentList({ incidents }: { incidents: IncidentSummary[] }) {
  if (incidents.length === 0) {
    return (
      <StatePanel title="No incidents yet">
        <p>The local API is reachable, but no incident records have been created. Start a seeded scenario to create the first run.</p>
      </StatePanel>
    );
  }

  return (
    <ul className="incident-list" aria-label="Incidents">
      {incidents.map((incident) => (
        <li key={incident.incident_id}>
          <Link className="incident-row" to={`/incidents/${encodeURIComponent(incident.incident_id)}`}>
            <span className="incident-row-main">
              <span className="incident-id">{incident.incident_id}</span>
              <span className="incident-scenario">{incident.scenario_id}</span>
            </span>
            <StatusBadge tone={statusTone(incident.status)}>{formatStatus(incident.status)}</StatusBadge>
            <time dateTime={incident.updated_at}>{formatTime(incident.updated_at)}</time>
            <span className="row-arrow" aria-hidden="true">↗</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function OverviewPage() {
  const [requestState, setRequestState] = useState<RequestState>("loading");
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => {
    let active = true;
    void listIncidents({ limit: 20 })
      .then((response) => {
        if (!active) return;
        setIncidents(response.items);
        setNextCursor(response.next_cursor ?? null);
        setRequestState(response.items.length === 0 ? "empty" : "ready");
      })
      .catch((error: unknown) => {
        if (!active) return;
        setErrorMessage(error instanceof Error ? error.message : "The local API could not be reached.");
        setRequestState("error");
      });
    return () => { active = false; };
  }, []);

  async function handleLoadMore() {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    setErrorMessage("");
    try {
      const response = await listIncidents({ limit: 20, cursor: nextCursor });
      setIncidents((current) => [...current, ...response.items]);
      setNextCursor(response.next_cursor ?? null);
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "The next page could not be loaded.");
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <div className="page-stack">
      <section className="page-heading" aria-labelledby="overview-title">
        <div>
          <h1 id="overview-title">Incident console</h1>
        <p className="heading-copy">Test a fault, review the proposed change, and verify the result from simulator state.</p>
        </div>
        <StatusBadge tone={requestState === "error" ? "error" : requestState === "ready" || requestState === "empty" ? "success" : "neutral"}>
          {requestState === "error" ? "API unavailable" : requestState === "loading" ? "Connecting" : "API available"}
        </StatusBadge>
      </section>

      <div className="overview-grid">
        <ScenarioLauncher />
        <AuthorityPanel />
      </div>

      <section className="ledger-section" aria-labelledby="incidents-title">
        <div className="section-heading">
          <div>
            <h2 id="incidents-title">Recent incidents</h2>
          </div>
          <span className="section-count">{incidents.length.toString().padStart(2, "0")} records</span>
        </div>
        {requestState === "loading" ? (
          <StatePanel title="Loading local state"><p>Checking the incident index.</p></StatePanel>
        ) : requestState === "error" ? (
          <StatePanel title="The incident index is unavailable" tone="error">
            <p>{errorMessage}</p>
            <p className="state-note">No incident state is inferred from the browser when the API is unavailable.</p>
          </StatePanel>
        ) : (
          <>
            {incidents.length > 0 && (
              <div className="incident-table-head" aria-hidden="true">
                <span>Incident</span>
                <span>State</span>
                <span>Updated</span>
                <span />
              </div>
            )}
            <IncidentList incidents={incidents} />
            {nextCursor && (
              <div className="load-more-row">
                <button className="button button-secondary" type="button" onClick={() => void handleLoadMore()} disabled={loadingMore}>
                  {loadingMore ? "Loading…" : "Load more incidents"}
                </button>
              </div>
            )}
            {errorMessage && <p className="inline-error" role="alert">{errorMessage}</p>}
          </>
        )}
      </section>

    </div>
  );
}

interface WorkspaceData {
  incident: IncidentDetailResponse;
  proposal: ProposalDetailResponse | null;
  proposalError: string;
}

async function fetchWorkspaceData(incidentId: string): Promise<WorkspaceData> {
  const incident = await getIncident(incidentId);
  const latestProposalId = incident.proposal_ids[0];
  if (!latestProposalId) {
    return { incident, proposal: null, proposalError: "" };
  }

  try {
    const proposal = await getProposal(latestProposalId);
    return { incident, proposal, proposalError: "" };
  } catch (error: unknown) {
    const proposalError = error instanceof Error ? error.message : "The latest proposal could not be loaded.";
    return { incident, proposal: null, proposalError };
  }
}

function PanelHeading({ title, meta }: { title: string; meta?: string }) {
  return (
    <div className="panel-heading">
      <h2>{title}</h2>
      {meta && <span className="panel-id">{meta}</span>}
    </div>
  );
}

function TimelinePanel({ events }: { events: TimelineEvent[] }) {
  return (
    <article className="ledger-panel timeline-panel">
      <PanelHeading title="Timeline" meta={`${events.length} events`} />
      {events.length === 0 ? (
        <StatePanel title="No timeline events recorded"><p>The incident exists, but the API returned no audit events for it.</p></StatePanel>
      ) : (
        <ol className="timeline-list">
          {events.map((event) => (
            <li className="timeline-event" key={event.event_id}>
              <span className="timeline-marker" aria-hidden="true" />
              <div className="timeline-event-content">
                <div className="timeline-event-heading">
                  <strong>{formatStatus(event.kind)}</strong>
                  <time dateTime={event.occurred_at}>{formatTime(event.occurred_at)}</time>
                </div>
                <p className="timeline-subject">{event.subject}</p>
                <details className="event-details">
                  <summary>Audit details</summary>
                  <pre>{JSON.stringify(event.detail, null, 2)}</pre>
                </details>
              </div>
            </li>
          ))}
        </ol>
      )}
    </article>
  );
}

function EvidencePanel({
  proposal,
  unavailable
}: {
  proposal: ProposalDetailResponse | null;
  unavailable: boolean;
}) {
  const evidenceIds = [...new Set(proposal?.evidence_ids ?? [])];
  return (
    <article className="ledger-panel">
      <PanelHeading title="Evidence references" meta={`${evidenceIds.length} ids`} />
      {unavailable ? (
        <StatePanel title="Evidence references unavailable" tone="warning">
          <p>Proposal details could not be loaded, so its evidence references cannot be inspected.</p>
        </StatePanel>
      ) : evidenceIds.length === 0 ? (
        <StatePanel title="No evidence references loaded"><p>No evidence identifiers are attached to the current proposal.</p></StatePanel>
      ) : (
        <>
          <ul className="reference-list">
            {evidenceIds.map((id) => <li key={id}><code>{id}</code></li>)}
          </ul>
          <p className="state-note evidence-note">These are evidence identifiers referenced by the proposal. This API does not return raw evidence payloads.</p>
        </>
      )}
    </article>
  );
}

function ProposalPanel({
  proposal,
  unavailableReason,
  busyAction,
  onApprove,
  onReject,
  onExecute
}: {
  proposal: ProposalDetailResponse | null;
  unavailableReason: string;
  busyAction: ActionKind | null;
  onApprove: () => void;
  onReject: (reason: string) => void;
  onExecute: () => void;
}) {
  const [rejectionReason, setRejectionReason] = useState("");

  if (!proposal) {
    return (
      <article className="ledger-panel">
        <PanelHeading title="Action proposal" meta="None" />
        {unavailableReason ? (
          <StatePanel title="Proposal details unavailable" tone="error">
            <p>{unavailableReason}</p>
            <p className="state-note">No approval or execution control is available until the authoritative proposal can be loaded.</p>
          </StatePanel>
        ) : (
          <StatePanel title="No action proposal loaded">
            <p>The investigation did not produce a proposal. No approval or execution control is available.</p>
          </StatePanel>
        )}
      </article>
    );
  }

  const expiresAt = Date.parse(proposal.expires_at);
  const isExpired = !Number.isFinite(expiresAt) || expiresAt <= Date.now();
  const preconditionsPassed = proposal.preconditions.length > 0 &&
    proposal.preconditions.every((condition) => condition.passed);
  const canDecide = proposal.state === "awaiting_approval" &&
    proposal.policy_decision === "require_approval" && !proposal.state_changed &&
    preconditionsPassed && !isExpired;
  const canExecute = proposal.state === "authorized" && !proposal.state_changed &&
    preconditionsPassed && !isExpired;

  function submitRejection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const reason = rejectionReason.trim();
    if (reason.length > 0) onReject(reason);
  }

  return (
    <article className="ledger-panel proposal-panel">
      <PanelHeading title="Action proposal" meta={`v${proposal.version}`} />
      <div className="proposal-state-line">
        <StatusBadge tone={statusTone(proposal.state)}>{formatStatus(proposal.state)}</StatusBadge>
        {proposal.policy_decision && <StatusBadge tone={proposal.policy_decision === "deny" ? "error" : "neutral"}>Policy: {formatStatus(proposal.policy_decision)}</StatusBadge>}
      </div>
      <dl className="detail-grid">
        <div><dt>Action</dt><dd>{formatStatus(proposal.action_type)}</dd></div>
        <div><dt>Target</dt><dd>{proposal.target_namespace}/{proposal.target_kind}/{proposal.target_name}</dd></div>
        <div><dt>Cluster</dt><dd>{proposal.target_cluster_id}</dd></div>
        <div><dt>Current replicas</dt><dd>{proposal.current_ready_replicas} ready / {proposal.current_desired_replicas} desired</dd></div>
        {proposal.proposed_replicas !== null && <div><dt>Proposed replicas</dt><dd>{proposal.proposed_replicas}</dd></div>}
        {proposal.proposed_revision !== null && <div><dt>Proposed revision</dt><dd>{proposal.proposed_revision}</dd></div>}
        <div><dt>Expires</dt><dd>{formatTime(proposal.expires_at)}</dd></div>
      </dl>

      {proposal.state_changed && (
        <StatePanel title="Live state changed since proposal" tone="warning">
          <p>The proposal fingerprint no longer matches the current simulated state. Approval and execution are disabled; the gateway will independently re-check execution preconditions.</p>
        </StatePanel>
      )}
      {!preconditionsPassed && !proposal.state_changed && (
        <StatePanel title="Proposal preconditions are not satisfied" tone="warning">
          <p>At least one required precondition is missing or failed. Approval and execution are disabled.</p>
        </StatePanel>
      )}
      {isExpired && <StatePanel title="Proposal has expired" tone="warning"><p>The proposal expiry time has passed or the expiry timestamp is invalid. Request a fresh scenario run instead of approving this proposal.</p></StatePanel>}
      {proposal.policy_reason_codes.length > 0 && (
        <div className="reason-codes"><span className="subsection-label">Policy reason codes</span><ul>{proposal.policy_reason_codes.map((reason) => <li key={reason}><code>{reason}</code></li>)}</ul></div>
      )}

      <div className="precondition-section">
        <span className="subsection-label">Preconditions</span>
        {proposal.preconditions.length === 0 ? <p className="state-note">No preconditions were returned.</p> : (
          <ul className="precondition-list">
            {proposal.preconditions.map((condition) => (
              <li key={condition.name}>
                <StatusBadge tone={condition.passed ? "success" : "error"}>{condition.passed ? "Pass" : "Fail"}</StatusBadge>
                <div><strong>{formatStatus(condition.name)}</strong><span>Expected: {condition.expected}</span><span>Observed: {condition.observed}</span></div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {proposal.expected_postconditions.length > 0 && (
        <div className="precondition-section">
          <span className="subsection-label">Expected postconditions</span>
          <ul className="plain-list">{proposal.expected_postconditions.map((condition) => <li key={condition}>{condition}</li>)}</ul>
        </div>
      )}

      {canDecide && (
        <div className="decision-controls">
          <p className="subsection-label">Human decision required</p>
          <p className="state-note">Approval records the decision. It does not execute the action. This local API uses its fixed demo principal; this form is not user authentication.</p>
          <div className="decision-buttons">
            <button className="button button-primary" type="button" onClick={onApprove} disabled={busyAction !== null}>
              {busyAction === "approve" ? "Recording approval…" : "Approve proposal"}
            </button>
          </div>
          <form className="rejection-form" onSubmit={submitRejection}>
            <label className="field"><span>Rejection reason</span><textarea value={rejectionReason} onChange={(event) => setRejectionReason(event.target.value)} maxLength={500} rows={2} placeholder="Explain why this action should not proceed." /></label>
            <button className="button button-danger" type="submit" disabled={busyAction !== null || rejectionReason.trim().length === 0}>
              {busyAction === "reject" ? "Recording rejection…" : "Reject proposal"}
            </button>
          </form>
        </div>
      )}

      {canExecute && (
        <div className="decision-controls execution-controls">
          <p className="subsection-label">Approved action</p>
          <p className="state-note">The action is authorized. Execution still passes through the backend gateway and independent verifier.</p>
          <button className="button button-primary" type="button" onClick={onExecute} disabled={busyAction !== null}>
            {busyAction === "execute" ? "Executing through gateway…" : "Execute approved action"}
          </button>
        </div>
      )}

      {proposal.state === "denied" && <StatePanel title="Policy denied this proposal" tone="error"><p>The policy engine did not authorize this action. The frontend cannot override that decision.</p></StatePanel>}
      {proposal.state === "not_recovered" && <StatePanel title="Verification did not confirm recovery" tone="error"><p>The action may have executed, but the configured postconditions were not satisfied.</p></StatePanel>}
      {proposal.state === "escalated" && <StatePanel title="Outcome requires operator attention" tone="warning"><p>The system escalated this proposal. Do not interpret an executed write as proof of recovery.</p></StatePanel>}
    </article>
  );
}

function ExecutionPanel({ result }: { result: ExecuteResponse | null }) {
  if (!result) return null;
  const status = result.verification_status;
  const title = status === "recovered"
    ? "Verifier confirmed recovery"
    : status === "not_recovered"
      ? "Verifier did not confirm recovery"
      : status === "unknown"
        ? "Verification outcome is unknown"
        : result.reason_code === "execution_unknown"
          ? "Execution outcome is unknown"
          : "Execution response received";
  return (
    <section className={`execution-result execution-${verificationTone(status)}`} aria-labelledby="execution-result-title">
      <div className="execution-result-heading">
        <h2 id="execution-result-title">{title}</h2>
        <StatusBadge tone={verificationTone(status)}>{status ? formatStatus(status) : formatStatus(result.reason_code)}</StatusBadge>
      </div>
      <dl className="detail-grid">
        <div><dt>Executed</dt><dd>{result.executed ? "Yes" : "No"}</dd></div>
        <div><dt>Attempt</dt><dd>{result.attempt_id ?? "Not created"}</dd></div>
        <div><dt>Writer operations</dt><dd>{result.writer_operations}</dd></div>
        <div><dt>Proposal state</dt><dd>{result.proposal_state ? formatStatus(result.proposal_state) : "Not returned"}</dd></div>
      </dl>
      <p className="state-note">Recovery is shown only when the independent verifier returns <code>recovered</code>. A missing or unknown verification result remains indeterminate.</p>
    </section>
  );
}

function IncidentWorkspacePage() {
  const { incidentId } = useParams<{ incidentId: string }>();
  const [requestState, setRequestState] = useState<RequestState>(incidentId ? "loading" : "empty");
  const [incident, setIncident] = useState<IncidentDetailResponse | null>(null);
  const [proposal, setProposal] = useState<ProposalDetailResponse | null>(null);
  const [proposalError, setProposalError] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [busyAction, setBusyAction] = useState<ActionKind | null>(null);
  const [actionError, setActionError] = useState("");
  const [actionAnnouncement, setActionAnnouncement] = useState("");
  const [executionResult, setExecutionResult] = useState<ExecuteResponse | null>(null);

  useEffect(() => {
    if (!incidentId) {
      setIncident(null);
      setProposal(null);
      setProposalError("");
      setErrorMessage("");
      setActionError("");
      setExecutionResult(null);
      setBusyAction(null);
      setRequestState("empty");
      return;
    }
    let active = true;
    setRequestState("loading");
    setErrorMessage("");
    setProposalError("");
    setExecutionResult(null);
    setActionError("");
    setBusyAction(null);
    void fetchWorkspaceData(incidentId)
      .then((data) => {
        if (!active) return;
        setIncident(data.incident);
        setProposal(data.proposal);
        setProposalError(data.proposalError);
        setRequestState("ready");
      })
      .catch((error: unknown) => {
        if (!active) return;
        setErrorMessage(error instanceof Error ? error.message : "The incident could not be loaded.");
        setRequestState("error");
      });
    return () => { active = false; };
  }, [incidentId]);

  async function refreshWorkspace() {
    if (!incidentId) return;
    const data = await fetchWorkspaceData(incidentId);
    setIncident(data.incident);
    setProposal(data.proposal);
    setProposalError(data.proposalError);
    setRequestState("ready");
  }

  async function handleDecision(decision: "approve" | "reject", reason?: string) {
    if (!proposal) return;
    setBusyAction(decision);
    setActionError("");
    setActionAnnouncement("");
    try {
      await decideProposal(proposal.proposal_id, {
        decision,
        expected_version: proposal.version,
        ...(decision === "reject" ? { reason } : {})
      });
    } catch (error: unknown) {
      setActionError(
        error instanceof ApiError && error.problem
          ? `${error.problem.code}: ${error.message}`
          : error instanceof Error ? error.message : "The decision could not be recorded."
      );
      setBusyAction(null);
      return;
    }

    try {
      await refreshWorkspace();
      setActionAnnouncement(
        decision === "approve"
          ? "Proposal approved. Execution remains a separate step."
          : "Proposal rejected. The incident state was not changed by this decision."
      );
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : "the workspace could not be refreshed";
      setActionError(`The decision was recorded, but the latest workspace state could not be loaded: ${detail}`);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleExecute() {
    if (!proposal) return;
    setBusyAction("execute");
    setActionError("");
    setActionAnnouncement("");
    try {
      const result = await executeProposal(proposal.proposal_id);
      setExecutionResult(result);
      setActionAnnouncement(
        result.verification_status === "recovered"
          ? "Execution completed and independent verification confirmed recovery."
          : `Execution response received. Verification status: ${result.verification_status ?? "unavailable"}.`
      );
    } catch (error: unknown) {
      setActionError(
        error instanceof ApiError && error.problem
          ? `${error.problem.code}: ${error.message}`
          : error instanceof Error
            ? error.message
            : "The gateway response could not be confirmed. Check the incident timeline before retrying."
      );
      setBusyAction(null);
      return;
    }

    try {
      await refreshWorkspace();
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : "the workspace could not be refreshed";
      setActionError(`The gateway response was received, but the latest workspace state could not be loaded: ${detail}`);
    } finally {
      setBusyAction(null);
    }
  }

  const workspaceStatus = requestState === "error" ? "Disconnected" : requestState === "loading" ? "Loading" : incident ? formatStatus(incident.status) : "No record loaded";

  return (
    <div className="page-stack">
      <section className="page-heading workspace-heading" aria-labelledby="workspace-title">
        <div>
          <h1 id="workspace-title">{incidentId ?? "No incident selected"}</h1>
          <p className="heading-copy">The API is the source of truth for incident state, proposal authorization, and verification. The browser does not infer a successful remediation.</p>
        </div>
        <StatusBadge tone={requestState === "error" ? "error" : incident ? statusTone(incident.status) : "neutral"}>{workspaceStatus}</StatusBadge>
      </section>

      {requestState === "loading" && <StatePanel title="Loading incident state"><p>Reading the incident, audit timeline, and associated proposal records.</p></StatePanel>}
      {requestState === "error" && (
        <StatePanel title="Incident state unavailable" tone="error">
          <p>{errorMessage}</p>
          <Link className="text-link" to="/overview">Return to overview</Link>
        </StatePanel>
      )}
      {requestState === "empty" && (
        <StatePanel title="No incident selected">
          <p>Choose an incident from the overview, or start a seeded scenario run.</p>
          <Link className="text-link" to="/overview">View incident index</Link>
        </StatePanel>
      )}

      {incident && requestState === "ready" && (
        <>
          <section className="incident-summary-strip" aria-label="Incident summary">
            <div><span>Scenario</span><strong>{incident.scenario_id}</strong></div>
            <div><span>Opened</span><strong>{formatTime(incident.opened_at)}</strong></div>
            <div><span>Environment</span><strong>{incident.environment_id}</strong></div>
            <div><span>Verification</span><strong><StatusBadge tone={verificationTone(incident.verification_status)}>{incident.verification_status ? formatStatus(incident.verification_status) : "Not run / unavailable"}</StatusBadge></strong></div>
          </section>

          <section className="workspace-grid" aria-label="Incident workspace panels">
            <div className="workspace-primary">
              <ProposalPanel
                proposal={proposal}
                unavailableReason={proposalError}
                busyAction={busyAction}
                onApprove={() => void handleDecision("approve")}
                onReject={(reason) => void handleDecision("reject", reason)}
                onExecute={() => void handleExecute()}
              />
              <TimelinePanel events={incident.timeline} />
            </div>
            <aside className="workspace-secondary" aria-label="Incident context">
              <EvidencePanel proposal={proposal} unavailable={Boolean(proposalError)} />
              <ExecutionPanel result={executionResult} />
              {incident.terminal_reason && (
                <p className="terminal-reason"><strong>Terminal reason</strong>{incident.terminal_reason}</p>
              )}
            </aside>
          </section>
          {actionError && <p className="inline-error action-error" role="alert">{actionError}</p>}
          <p className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
            {actionAnnouncement}
          </p>
        </>
      )}
    </div>
  );
}

function NotFoundPage() {
  return (
    <div className="page-stack">
      <section className="page-heading" aria-labelledby="not-found-title">
        <div>
          <h1 id="not-found-title">That surface is not wired.</h1>
          <p className="heading-copy">Return to the overview to see the available local workspace surfaces.</p>
        </div>
      </section>
      <Link className="text-link" to="/overview">Go to overview</Link>
    </div>
  );
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<Navigate replace to="/overview" />} />
          <Route path="overview" element={<OverviewPage />} />
          <Route path="incidents" element={<IncidentWorkspacePage />} />
          <Route path="incidents/:incidentId" element={<IncidentWorkspacePage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
