import { useEffect, useState, type ReactNode } from "react";
import {
  BrowserRouter,
  Link,
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
  useParams
} from "react-router-dom";

import { getIncident, listIncidents } from "./api/client";
import type { IncidentSummary } from "./api/types";

type RequestState = "loading" | "ready" | "empty" | "error";
type StatusTone = "neutral" | "warning" | "error";

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

function formatStatus(status: string): string {
  return status.replaceAll("_", " ");
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(new Date(value));
}

function AppLayout() {
  return (
    <div className="app-frame">
      <aside className="side-rail" aria-label="Faultweld navigation">
        <NavLink className="brand-lockup" to="/overview" aria-label="Faultweld overview">
          <span className="brand-mark" aria-hidden="true">
            FW
          </span>
          <span>
            <strong>Faultweld</strong>
            <small>local operator console</small>
          </span>
        </NavLink>
        <div className="rail-rule" />
        <p className="rail-label">Workspace</p>
        <nav className="primary-nav" aria-label="Primary navigation">
          <NavLink
            className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}
            end
            to="/overview"
          >
            <span className="nav-index">01</span>
            Overview
          </NavLink>
          <NavLink
            className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}
            to="/incidents"
          >
            <span className="nav-index">02</span>
            Incident workspace
          </NavLink>
        </nav>
        <div className="rail-footer">
          <span className="pulse-dot" aria-hidden="true" />
          <span>Read-only foundation</span>
        </div>
      </aside>
      <div className="main-frame">
        <header className="topbar">
          <div className="breadcrumb" aria-label="Breadcrumb">
            <span>Local demo</span>
            <span aria-hidden="true">/</span>
            <strong>Operator workspace</strong>
          </div>
          <div className="topbar-state">
            <span className="state-dot state-dot-neutral" aria-hidden="true" />
            <span>Synthetic environment</span>
          </div>
        </header>
        <main id="main-content" className="page-content">
          <Outlet />
        </main>
        <footer className="app-footer">
          <span>Faultweld foundation build</span>
          <span>No external systems connected</span>
        </footer>
      </div>
    </div>
  );
}

function OverviewPage() {
  const [requestState, setRequestState] = useState<RequestState>("loading");
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    let mounted = true;
    void listIncidents()
      .then((response) => {
        if (!mounted) {
          return;
        }
        setIncidents(response.items);
        setRequestState(response.items.length === 0 ? "empty" : "ready");
      })
      .catch((error: unknown) => {
        if (!mounted) {
          return;
        }
        setErrorMessage(error instanceof Error ? error.message : "The local API could not be reached.");
        setRequestState("error");
      });
    return () => {
      mounted = false;
    };
  }, []);

  return (
    <div className="page-stack">
      <section className="page-heading" aria-labelledby="overview-title">
        <div>
          <p className="eyebrow">Operator overview</p>
          <h1 id="overview-title">Incident response, at a glance.</h1>
          <p className="heading-copy">
            A clear view of local incident state. Evidence, authorization, and verified recovery will appear
            here as those parts of the system come online.
          </p>
        </div>
        <StatusBadge tone={requestState === "error" ? "error" : "neutral"}>
          {requestState === "error" ? "Disconnected" : "Local read-only"}
        </StatusBadge>
      </section>

      <section className="ledger-section" aria-labelledby="incidents-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Read-only index</p>
            <h2 id="incidents-title">Recent incidents</h2>
          </div>
          <span className="section-count">{incidents.length.toString().padStart(2, "0")} records</span>
        </div>
        {requestState === "loading" ? (
          <StatePanel title="Loading local state">
            <p>Checking the read-only incident index.</p>
          </StatePanel>
        ) : requestState === "error" ? (
          <StatePanel title="The incident index is unavailable" tone="error">
            <p>{errorMessage}</p>
            <p className="state-note">
              The workspace remains disconnected. No incident state is being inferred from the browser.
            </p>
          </StatePanel>
        ) : requestState === "empty" ? (
          <StatePanel title="No incidents yet">
            <p>
              The local API is reachable, but no incident records have been created. Start a seeded scenario
              from a later build; this shell does not create or mutate one.
            </p>
          </StatePanel>
        ) : (
          <ul className="incident-list" aria-label="Incidents">
            {incidents.map((incident) => (
              <li key={incident.incident_id}>
                <Link className="incident-row" to={`/incidents/${encodeURIComponent(incident.incident_id)}`}>
                  <span className="incident-id">{incident.incident_id}</span>
                  <span className="incident-scenario">{incident.scenario_id}</span>
                  <StatusBadge tone={incident.status === "unknown" ? "warning" : "neutral"}>
                    {formatStatus(incident.status)}
                  </StatusBadge>
                  <time dateTime={incident.updated_at}>{formatTime(incident.updated_at)}</time>
                  <span className="row-arrow" aria-hidden="true">
                    ↗
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="principles-grid" aria-label="Faultweld operating principles">
        <article className="principle-block">
          <span className="principle-index">A</span>
          <h2>Evidence before claims</h2>
          <p>Model explanations will never be rendered as verified system state.</p>
        </article>
        <article className="principle-block">
          <span className="principle-index">B</span>
          <h2>Authority stays narrow</h2>
          <p>Approval and execution controls will appear only when the API can support them.</p>
        </article>
        <article className="principle-block">
          <span className="principle-index">C</span>
          <h2>Unknown stays visible</h2>
          <p>Disconnected and indeterminate states are never collapsed into a success signal.</p>
        </article>
      </section>
    </div>
  );
}

function IncidentWorkspacePage() {
  const { incidentId } = useParams<{ incidentId: string }>();
  const [requestState, setRequestState] = useState<RequestState>(incidentId ? "loading" : "empty");
  const [incident, setIncident] = useState<IncidentSummary | null>(null);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    if (incidentId === undefined) {
      setRequestState("empty");
      setIncident(null);
      return;
    }

    let mounted = true;
    setRequestState("loading");
    void getIncident(incidentId)
      .then((response) => {
        if (!mounted) {
          return;
        }
        setIncident(response);
        setRequestState("ready");
      })
      .catch((error: unknown) => {
        if (!mounted) {
          return;
        }
        setErrorMessage(error instanceof Error ? error.message : "The incident could not be loaded.");
        setRequestState("error");
      });
    return () => {
      mounted = false;
    };
  }, [incidentId]);

  const workspaceStatus = requestState === "error" ? "Disconnected" : requestState === "loading" ? "Loading" : "No record loaded";
  const statusTone: StatusTone = requestState === "error" ? "error" : "neutral";

  return (
    <div className="page-stack">
      <section className="page-heading workspace-heading" aria-labelledby="workspace-title">
        <div>
          <p className="eyebrow">02 / Incident workspace</p>
          <h1 id="workspace-title">{incidentId ?? "No incident selected"}</h1>
          <p className="heading-copy">
            The workspace is ready for authoritative incident state. Until a read-only incident record is
            available, every panel remains explicitly empty.
          </p>
        </div>
        <StatusBadge tone={statusTone}>{workspaceStatus}</StatusBadge>
      </section>

      <section className="workspace-grid" aria-label="Incident workspace panels">
        <article className="ledger-panel timeline-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Run record</p>
              <h2>Timeline</h2>
            </div>
            <span className="panel-id">{incident?.incident_id ?? "Awaiting selection"}</span>
          </div>
          {requestState === "loading" ? (
            <StatePanel title="Loading incident state">
              <p>Reading the incident contract from the local API.</p>
            </StatePanel>
          ) : requestState === "error" ? (
            <StatePanel title="Incident state unavailable" tone="error">
              <p>{errorMessage}</p>
              <Link className="text-link" to="/overview">
                Return to overview
              </Link>
            </StatePanel>
          ) : requestState === "ready" ? (
            <StatePanel title="Timeline events are not in this contract">
              <p>
                The incident summary loaded, but the current read-only API does not expose event history.
              </p>
            </StatePanel>
          ) : (
            <StatePanel title="No incident selected">
              <p>Choose an incident from the overview, or open this workspace with an incident identifier.</p>
              <Link className="text-link" to="/overview">
                View incident index
              </Link>
            </StatePanel>
          )}
        </article>

        <article className="ledger-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Source context</p>
              <h2>Evidence</h2>
            </div>
            <span className="panel-id">Not loaded</span>
          </div>
          <StatePanel title="No evidence loaded">
            <p>Evidence will appear with source, observation time, and freshness when that API is connected.</p>
          </StatePanel>
        </article>

        <article className="ledger-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Authority boundary</p>
              <h2>Action context</h2>
            </div>
            <span className="panel-id">Read-only</span>
          </div>
          <StatePanel title="No action proposal loaded">
            <p>
              This foundation has no approval or execution controls. A proposal will be shown only after the
              backend exposes an authoritative read-only contract.
            </p>
          </StatePanel>
        </article>
      </section>
    </div>
  );
}

function NotFoundPage() {
  return (
    <div className="page-stack">
      <section className="page-heading" aria-labelledby="not-found-title">
        <div>
          <p className="eyebrow">Route not found</p>
          <h1 id="not-found-title">That surface is not wired.</h1>
          <p className="heading-copy">Return to the overview to see the available local workspace surfaces.</p>
        </div>
      </section>
      <Link className="text-link" to="/overview">
        Go to overview
      </Link>
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
