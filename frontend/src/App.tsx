const foundationItems = [
  "Backend health endpoint",
  "Frontend application shell",
  "Strict TypeScript configuration",
  "Pinned dependency lockfiles"
] as const;

function App() {
  return (
    <main className="app-shell">
      <section className="intro" aria-labelledby="page-title">
        <p className="eyebrow">Faultweld</p>
        <h1 id="page-title">Foundation scaffold</h1>
        <p className="lede">
          The local application layout is ready. Incident workflows and remediation features will be added
          in later planned commits.
        </p>
      </section>
      <section className="status-panel" aria-labelledby="status-title">
        <div>
          <p className="eyebrow">Current state</p>
          <h2 id="status-title">Scaffold only</h2>
        </div>
        <span className="status-badge">Not connected</span>
        <ul>
          {foundationItems.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>
    </main>
  );
}

export default App;
