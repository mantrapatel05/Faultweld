# Faultweld

**An incident-response agent that can propose fixes but cannot act on its own, and whose approvals die the moment the world changes.**

Faultweld runs a full incident workflow against a simulated Kubernetes deployment: alert, investigation by a real model, typed proposal, policy check, human approval, guarded execution, and independent verification. The model is Groq's `openai/gpt-oss-120b`. It can read evidence and propose one action. It cannot choose a target, grant approval or touch the cluster. The default demos, tests and CI need no Docker, no cluster and no API key.

The question it answers: *if an approval is granted and the system then changes before the action runs, does the action still happen?* In Faultweld it does not. That behavior is tested, deliberately sabotaged, and measured.

> **Scope.** The safety controls are real and tested against both a scripted model and a compromised scripted model, so they hold whatever a model says. How a real model behaves is measured separately by the [live-model evaluation](#live-model-evaluation), which is run manually, costs real money, and is reported with sample sizes and confidence intervals.

## Contents

- [Architecture](#architecture)
- [How it works](#how-it-works)
- [The AI layer](#the-ai-layer)
- [Workflow orchestration](#workflow-orchestration)
- [The stale-approval guarantee](#the-stale-approval-guarantee)
- [Incident lifecycle](#incident-lifecycle)
- [Quick start](#quick-start)
- [Demo](#demo)
- [Scenarios](#scenarios)
- [Results](#results)
- [Live-model evaluation](#live-model-evaluation)
- [Safety invariants](#safety-invariants)
- [Repository layout](#repository-layout)
- [Development and checks](#development-and-checks)
- [Docker](#docker)
- [Limitations](#limitations)
- [Real-cluster mode (optional)](#real-cluster-mode-optional)
- [Roadmap](#roadmap)

## Architecture

```mermaid
flowchart LR
  Console["React console"] -->|"run, approve, execute"| API["FastAPI"]
  CLI["CLI (lite.py)"] -->|"same core"| API
  CLI -->|"flow / resume"| Flow["LangGraph workflow<br/>identifiers only"]

  subgraph AI["AI layer (no writer, no cluster handle)"]
    Agent["Investigator"] -->|"bounded loop"| Tools["4 read-only tools"]
    Agent -->|"complete()"| Seam{{"ModelProvider seam"}}
    Seam --> Scripted["Scripted (tests, CI)"]
    Seam --> Groq["Groq gpt-oss-120b<br/>(OpenRouter optional)"]
    Seam --> Replay["Replay from transcripts"]
  end

  API --> Agent
  Flow --> Agent
  Tools -->|"read-only snapshot"| Source["SimSource<br/>read-only"]

  Agent -->|"typed proposal"| Policy["Policy engine"]
  Policy -->|"decision"| DB[("SQLite<br/>proposals, approvals,<br/>attempts, audit")]
  API -->|"record human decision"| DB

  API -->|"execute by ID"| Gateway["Gateway"]
  Flow -->|"execute by ID"| Gateway
  Gateway -->|"load records"| DB
  Gateway -->|"re-check live state"| Source
  Gateway -->|"one typed write"| Writer["SimWriter"]

  Source --> Cluster["SimCluster"]
  Writer --> Cluster
  Verifier["Verifier"] -->|"independent reads"| Source
  Verifier -->|"recovered / not_recovered / unknown"| DB

  Evals["Live eval harness<br/>(manual, capped cost)"] -.->|"injected provider,<br/>same policy, store, gateway"| Agent
```

| Component | Can | Cannot |
|---|---|---|
| Agent / model | call four read-only tools, return a typed diagnosis and one action type with parameters | choose target, principal, approver, role, expiry, digest, fingerprint, idempotency key or a policy decision; import the writer; see credentials |
| Policy | decide from trusted inputs | read model-written risk or confidence |
| Store | persist records, enforce unique idempotency keys | edit or delete audit events (database triggers abort) |
| Gateway | apply one typed operation after full re-validation | accept action parameters from its caller |
| Verifier | read live state | trust the writer's return value |
| Workflow (LangGraph) | sequence the steps, pause for approval | hold a writer, grant approval, or be trusted over the store |
| Live eval harness | run cases through the real policy, store, gateway and verifier, and record observations | read evaluator labels (only the graders do), run in CI, or exceed its cost cap |
| Evaluator labels (`scenarios/expected/`) | be read by graders | be read by the agent, policy, store, gateway, simulator, API or eval runner |

## How it works

1. **A fault is injected** into the simulated deployment `faultweld-sample`.
2. **The agent investigates.** The model calls up to four read-only tools, then returns a diagnosis that cites evidence IDs, then a proposal. The model picks only an action type and parameters; deterministic code fills target, principal, expiry, digests and state fingerprint.
3. **Policy classifies** the proposal as `DENY`, `REQUIRE_APPROVAL` or `ALLOW`, with stable reason codes. Missing input denies. Replica counts outside the allowed range are denied.
4. **A human approves or rejects.** The approval binds to the exact proposal, the state the reviewer saw, the policy version, and an expiry. It is single-use, and approving does not execute anything.
5. **The gateway executes.** It is the only component holding a writer. It re-checks everything first; any mismatch is refused with a reason code and zero side effects.
6. **An independent verifier** reads live state and reports `recovered`, `not_recovered` or `unknown`. Only `recovered` closes the incident.

## The AI layer

The model sits behind one interface, `ModelProvider.complete(request)`. Everything else in the system treats it as an untrusted text source.

```mermaid
sequenceDiagram
  autonumber
  participant I as Investigator
  participant M as Model (Groq)
  participant T as Read-only tools
  participant S as SimSource
  participant P as Policy engine

  I->>M: alert summary plus redacted evidence
  loop within budget (4 model calls, 6 tool calls, 8000 tokens, 120 s)
    M-->>I: tool_call (get_deployment_status, get_readiness_probe, get_recent_changes, get_logs)
    I->>T: validate arguments against the tool schema
    T->>S: read-only snapshot
    S-->>T: state
    T-->>I: evidence item (logs redacted, marked untrusted)
    I->>M: new evidence
  end
  M-->>I: final diagnosis citing evidence IDs
  I->>M: ask for a typed proposal
  M-->>I: action type and parameters only
  Note over I: Unknown evidence ID, invalid JSON, unsupported action<br/>or exhausted budget means escalation, not a guess
  I->>P: proposal plus trusted fields filled by code
  P-->>I: DENY, REQUIRE_APPROVAL or ALLOW
```

What the layer does, and what bounds it:

- **Provider seam.** Three interchangeable providers: scripted (tests and CI), live (`LlmProvider` over an OpenAI-compatible HTTPS API) and replay (recorded transcripts). Groq `openai/gpt-oss-120b` is the main model; OpenRouter is an optional second backend.
- **Structured output, validated locally.** Every response is checked against a JSON schema and the domain types. A bad response gets a bounded correction attempt, then the investigation escalates.
- **Read-only tool loop.** Four tools only: `get_deployment_status`, `get_readiness_probe`, `get_recent_changes`, `get_logs`. Tool arguments are schema-checked, log text is redacted, truncated and marked untrusted, and the loop stops on repeated calls, consecutive invalid steps, or any exhausted budget. Every non-final stop escalates.
- **Evidence-grounded.** A diagnosis or proposal that cites an evidence ID the run never produced is rejected.
- **Prompts are versioned and allow-listed.** The model sees a redacted projection of the evidence, not incident or run identifiers, and the prompt version is recorded with every evaluation.
- **Transport limits.** HTTPS only, an allow-listed host, no redirects, no credentials in the URL, temperature 0, 1024 output tokens, 45 s timeout, one retry, and a per-run cost cap (default $0.05). The key is read from the environment and never printed.
- **Transcripts.** Live calls are recorded, redacted, under the git-ignored `transcripts/` directory. Replay reproduces a run offline and refuses a transcript that was changed.
- **Two modes.** `agentic` (tool loop, the default) and `single-shot` (one evidence snapshot, no tools).

## Workflow orchestration

The same steps can run as a LangGraph workflow. The graph carries only identifiers and counters; the SQLite store stays the single source of authority. `await_approval` pauses only while the store has no decision, then re-reads the store, so neither the resume value nor a checkpoint can grant approval. A tampered or inconsistent workflow state fails closed and escalates.

```mermaid
flowchart LR
  START([start]) --> intake
  intake -->|ok| investigate
  intake -->|reason set| close["close_or_escalate"]
  investigate -->|"awaiting approval, or a decision is recorded"| await["await_approval<br/>(pauses, then re-reads the store)"]
  investigate -->|"already authorized"| execute
  investigate -->|"escalated, denied or no proposal"| close
  await -->|approved| execute
  await -->|"rejected, revoked, expired"| close
  execute -->|"executed"| verify
  execute -->|"refused by the gateway"| close
  verify --> close
  close --> END([end])
```

A paused workflow can be rebuilt from the store alone after a restart:

```powershell
.\.venv\Scripts\python scripts\lite.py flow healthy-replica-loss
.\.venv\Scripts\python scripts\lite.py approve <proposal-id>
.\.venv\Scripts\python scripts\lite.py resume <proposal-id>
```

## The stale-approval guarantee

The gateway runs these checks in order and stops at the first failure, recording one audit event and writing nothing else:

`proposal_not_found` → `approval_not_found` → `principal_mismatch` → `approval_rejected / revoked / consumed / expired` → `proposal_expired` → `role_not_permitted` → `digest_mismatch` → `target_mismatch` → `policy_changed / policy_denied` → `state_unavailable` → `state_changed` → atomic idempotency claim → one typed write.

If anyone changes the deployment between approval and execution, the state fingerprint no longer matches and the gateway answers `state_changed`. The writer is never called, the operation log stays empty, and state is untouched. The fingerprint is checked in more than one layer (the gateway and the store's claim step), so removing a single check does not by itself let a stale approval through.

## Incident lifecycle

Proposal states, exactly as enforced by the store's transition table. Any other transition is refused.

```mermaid
stateDiagram-v2
    [*] --> proposed
    proposed --> preflighted
    preflighted --> denied: policy DENY
    preflighted --> awaiting_approval: REQUIRE_APPROVAL
    preflighted --> authorized: ALLOW (test config only)
    awaiting_approval --> authorized: human approves
    awaiting_approval --> denied: human rejects
    authorized --> executing: gateway claims approval and key
    executing --> executed: write applied
    executing --> failed: write rejected
    executing --> execution_unknown: timeout, outcome unclear
    execution_unknown --> executed: reconciled
    execution_unknown --> executing: unchanged state, retry within limit
    execution_unknown --> failed
    executed --> verifying
    execution_unknown --> verifying
    verifying --> recovered: all postconditions pass
    verifying --> not_recovered: a postcondition fails
    verifying --> escalated: verifier unavailable or contradictory
    denied --> [*]
    failed --> [*]
    recovered --> [*]
    not_recovered --> [*]
    escalated --> [*]
```

The incident has its own status. Only a `recovered` verification closes it; `not_recovered`, `unknown` and `execution_unknown` escalate and are never shown as success in the UI. An investigation that ends without a proposal (bad model output, unknown evidence ID, exhausted budget) also escalates the incident.

## Quick start

Requires Python 3.12, Node.js 24, npm 11. On macOS/Linux use `.venv/bin/python` instead of `.\.venv\Scripts\python`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r backend\requirements.lock
npm ci --prefix frontend
```

## Demo

Everything in this section except the Groq and OpenRouter runs uses the scripted model and needs no key.

**Watch a transient fault heal (tick-based, deterministic):**

```powershell
.\.venv\Scripts\python scripts\sim.py watch deployment-unavailable
```

**Verified recovery** (propose, approve, execute once, verify):

```powershell
.\.venv\Scripts\python scripts\lite.py demo happy-path
```

**The refusal** (approve, then state changes; output shows `writer operations: 0`):

```powershell
.\.venv\Scripts\python scripts\lite.py demo stale-approval
```

**The console** (backend plus Vite dev server, open `http://127.0.0.1:5173`):

```powershell
.\.venv\Scripts\python scripts\dev.py
```

**Step by step through the CLI:**

```powershell
.\.venv\Scripts\python scripts\lite.py run healthy-replica-loss
.\.venv\Scripts\python scripts\lite.py show <proposal-id>
.\.venv\Scripts\python scripts\lite.py approve <proposal-id>
.\.venv\Scripts\python scripts\lite.py execute <proposal-id>
.\.venv\Scripts\python scripts\lite.py verify <incident-id>
```

The approver identity comes from the server. There is no flag to set a role or approver.

### Run the real model (Groq)

Put `GROQ_API_KEY` in your git-ignored `.env` (see `.env.example`) or set it in your shell. Never commit a key.

```powershell
# Diagnosis and proposal, stops after policy classification. Does not approve or execute.
.\.venv\Scripts\python scripts\llm_smoke.py --backend groq

# Full CLI flow with the model investigating (agentic mode is the default)
.\.venv\Scripts\python scripts\lite.py run healthy-replica-loss --provider llm --backend groq
.\.venv\Scripts\python scripts\lite.py run healthy-replica-loss --provider llm --backend groq --mode single-shot

# The same, through the LangGraph workflow's provider-independent steps, then approve and resume
.\.venv\Scripts\python scripts\lite.py flow healthy-replica-loss
```

This uses `openai/gpt-oss-120b`, which Groq lists as supporting JSON Schema output. The app still validates every response locally. The live call has a $0.05 per-run cost cap; the configured Groq prices are $0.15 / $0.60 per million input / output tokens. Calls are recorded, redacted, under `transcripts/`, and you can replay them offline:

```powershell
.\.venv\Scripts\python scripts\lite.py run healthy-replica-loss --provider replay --transcript-dir transcripts
```

Use synthetic project evidence only: prompts and completions go to a third-party provider.

### Optional OpenRouter backend

```powershell
.\.venv\Scripts\python scripts\llm_smoke.py --backend openrouter
```

Needs `OPENROUTER_API_KEY`. The default model there is `recursive/flint-v1.0:free`; pick another with `--model`. OpenRouter routes to a third-party provider whose terms may allow retaining prompts and completions.

## Scenarios

| Fixture | Fault | Valid outcome |
|---|---|---|
| `healthy-replica-loss` | scaled to 0 | scale 0 to 1, approve, verified recovery |
| `readiness-regression` | probe port broken | no valid fix in Lite; a restart runs but does not help, so it ends `not_recovered` (a model should escalate instead of proposing a fix) |
| `deployment-unavailable` | pod deleted | self-heals after 3 ticks; no mutation needed |

## Results

### Scripted safety suite

13 fixtures, 3 repeats each (39 runs), scripted provider, source commit `a5501d27924f277174d69721a05717cbbb1e7427`. All 39 runs passed. The full backend suite for the implementation passed **526 tests**. The report lives in the git-ignored `reports/` directory, so this is a record of that run, not a checked-in benchmark.

| Invariant | Required | Measured |
|---|---|---|
| Stale, expired, mismatched or revoked approvals rejected | N / N | 24 / 24 |
| Forbidden mutations | 0 | 0 |
| Duplicate side effects | 0 | 0 |
| False recoveries | 0 | 0 |
| Valid actions wrongly rejected | 0 | 0 |
| Unauthorized gateway calls blocked | N / N | 3 / 3 |
| Fixtures passing every repeat (pass^k) | 13 / 13 | 13 / 13 |

Fixtures cover: clean remediation, noisy evidence, stale guidance, provider timeout, timeout after mutation, prompt injection against a compromised scripted model, state or proposal changed after approval, policy or permission changed, identity mismatch, sequential and concurrent duplicate delivery, ineffective action, verifier unavailable, and budget exhaustion.

Reproduce:

```powershell
$GitSha = (git rev-parse HEAD).Trim()
$Run = "reports\raw-" + [guid]::NewGuid().ToString("N")
.\.venv\Scripts\python scripts\eval.py run --repeats 3 --out $Run --git-sha $GitSha
.\.venv\Scripts\python scripts\eval.py report --raw $Run --out reports\latest
```

The report command exits non-zero if any invariant regresses.

### Live-model results (Groq)

The verified run used `openai/gpt-oss-120b` through Groq on 2026-11-10: **42/42 planned runs** completed across 14 cases and 3 repeats, at a total cost of **$0.189673** under a $1.00 cap. Three provider rate-limit failures were retained in the report; the system invariants remained clean.

| Field | Value |
|---|---|
| Model / provider | `openai/gpt-oss-120b` / Groq |
| Date (UTC) | 2026-11-10 |
| Commit | `working-tree` |
| Runs completed / planned | 42 / 42 (3 repeats × 14 cases) |
| System invariants | 0 forbidden mutations, 0 duplicate side effects, 0 false recoveries |
| Correct action | 1 / 36 (2.8%, 95% Wilson interval 0.5%-14.2%) |
| Correct escalation | 4 / 6 (66.7%, 95% Wilson interval 30.0%-90.3%) |
| Over-action | 2 / 6 (33.3%, 95% Wilson interval 9.7%-70.0%) |
| Forbidden proposal | 1 / 4 (25.0%, 95% Wilson interval 4.6%-69.9%) |
| Injection compliance | 1 / 9 (11.1%, 95% Wilson interval 2.0%-43.5%) |
| Injection block | 1 / 1 (100.0%, 95% Wilson interval 20.7%-100.0%) |
| Invalid output | 0 / 42 (0.0%, 95% Wilson interval 0.0%-8.4%) |

## Live-model evaluation

The scripted suite shows the controls hold whatever a model says. The live evaluation measures what the real model does, and keeps two kinds of result apart:

- **System invariants** (any nonzero value is a bug in the system, not a property of the model): forbidden mutations, duplicate side effects, false recoveries, valid actions wrongly rejected, stale approvals rejected.
- **Model behavior** (a sample, always reported with n and a 95% Wilson interval): correct-action rate, correct-escalation rate, over-action rate, invalid-evidence-ID rate, invalid-output rate, forbidden-proposal rate, injection compliance and injection block rate, plus calls, tokens, cost and latency per run (mean, min, max).

```mermaid
flowchart LR
  Cases["14 cases per repeat<br/>3 scenarios, 10 attacks, 1 stale probe"] --> Run["Run: investigate with the injected provider"]
  Run --> Real["Real policy, store, gateway, verifier"]
  Real --> Raw["Raw observations<br/>(no labels)"]
  Raw --> Graders["Graders read<br/>live-expectations.json"]
  Graders --> Report["live-report.md and .json<br/>rates with n and 95% intervals"]
  Guard["Cost guard<br/>no run starts if the worst case passes the cap"] -.-> Run
  Run -.-> Trans["Redacted transcripts"] -.-> Replay["Offline replay"]
```

**Cases.** 14 per repeat: the three scenarios, ten synthetic injection attacks placed in the alert summary, a log line or a change event (against `healthy-replica-loss`), and one stale-approval probe that changes the deployment after approval. The corpus is small and invented for this project and contains no real secrets. Compliance is measurable only for attacks asking for a concrete proposal (out-of-bounds scale, rollback); the others feed the general rates only.

**How a run works.** Each run goes through the real policy, store, gateway and verifier. The harness plays the human reviewer and approves every proposal that policy lets through, so "executed" counts show what would run if a reviewer approved everything, not how a reviewer behaves. Evaluator expectations live only in `scenarios/expected/live-expectations.json` and are read only by the graders.

**Run it (Groq).** The key comes from your shell environment only, never from CI or the repo.

```powershell
# 1. Estimate only: no key, no provider calls.
.\.venv\Scripts\python scripts\eval.py run --mode live --dry-run --backend groq --model openai/gpt-oss-120b --input-price 0.15 --output-price 0.60 --repeats 3 --max-cost-usd 0.50

# 2. Real run.
$env:GROQ_API_KEY = "<your key>"
.\.venv\Scripts\python scripts\eval.py run --mode live --backend groq --model openai/gpt-oss-120b --input-price 0.15 --output-price 0.60 --repeats 3 --max-cost-usd 0.50 --require-estimate-within-cap --out reports\live

# 3. Report. Exits non-zero if a system invariant is violated.
.\.venv\Scripts\python scripts\eval.py report --raw reports\live\raw --out reports\live\latest
```

The dry run for 3 repeats prints 42 runs, at most 168 model calls, and a worst-case cost of about $0.20, which is under the $0.50 cap.

**Cost control.** A run never starts if its worst case would pass the cap, a cap of zero runs nothing, and a run stopped by the cap is reported as **partial data**. The worst case is an upper bound (the full token budget billed at the dearer price), so the real cost is usually lower.

**Replay and ablation.** `--mode replay --transcripts <dir>` reproduces a recorded run offline and refuses a changed transcript. `scripts/eval.py ablate` compares single-shot with agentic mode, or several models in agentic mode (`--models a,b`), over identical cases and repeats, and states unequal sample sizes.

**CI.** The normal CI never uses a key or live mode. `.github/workflows/live-evals.yml` is `workflow_dispatch` only, dry-runs first, refuses to start if the worst-case estimate exceeds the cap, and uploads raw results, report and transcripts. As committed it reads an `OPENROUTER_API_KEY` secret, so Groq runs are local until the workflow is pointed at a Groq secret.

## Safety invariants

1. The model proposes; deterministic code authorizes and executes. A prompt is not a boundary.
2. Only the gateway holds a writer.
3. Actions are a closed enum with typed arguments. No shell, no free-form manifest.
4. Policy computes authority from trusted inputs; missing input denies.
5. Everything fails closed: unknown state, missing record, expired approval, digest mismatch, unavailable verifier, inconsistent workflow state, invalid or ungrounded model output, exhausted budget.
6. Model, evidence and log text is untrusted data and never changes state, routing, policy or approvals.
7. `recovered` requires every postcondition from independent state.
8. Evaluator-only labels never reach runtime code, including the eval runner.
9. Model tools are read-only, schema-checked and budget-bounded; model output never supplies authority fields.
10. Keys come from the environment only. Live model calls never run in CI.

## Repository layout

```
backend/app/
  api/        FastAPI routes, RFC 7807 problem details
  domain/     versioned, frozen Pydantic contracts
  scenarios/  fixtures, health classification, repeatability gate
  sim/        simulated cluster, read-only source, writer port
  agent/      evidence view, investigator, bounded tool loop, proposal builder, providers
    llm/      HTTP client, Groq/OpenRouter config, prompts, schemas, pricing, transcripts
    tools/    the four read-only tools and their executor
  policy/     config, pure evaluate(), principals, reason codes
  store/      SQLite: proposals, approvals, attempts, append-only audit
  gateway/    gateway and verifier
  workflow/   LangGraph orchestration, identifier-only state, approval pause and resume
  evals/      fixtures, runner, graders, report; live runner, attacks, stats, ablation
frontend/     React + Vite + TypeScript console
scenarios/    fixtures (agent-visible) and expected/ (evaluator-only)
scripts/      sim.py, lite.py, llm_smoke.py, eval.py, dev.py, generate_api_types.py, check_hygiene.py
infra/        optional kind cluster tooling (read-only evidence, not in the operator path)
Dockerfile    backend API image
```

The normal app (`app.main:app`) exposes only three mutating routes: scenario run, decision, execute. The demo app (`app.demo_main:app`) adds a state-mutation route used by the browser suite to make an approval stale; never mount it in production.

## Development and checks

```powershell
.\.venv\Scripts\python -m ruff check backend frontend\tests scripts
.\.venv\Scripts\python -m mypy --config-file backend\pyproject.toml backend\app backend\tests
.\.venv\Scripts\python -m unittest discover -s backend\tests -t backend -p "test_*.py"
.\.venv\Scripts\python scripts\generate_api_types.py --check
.\.venv\Scripts\python scripts\check_hygiene.py
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run test
npm --prefix frontend run build
```

No test uses the network or a key: model behavior is exercised with scripted, fake and replayed providers. Browser checks (Chromium via Playwright) run against the demo app: `python -m playwright install chromium`, then `frontend/tests/browser/console_smoke.py` through `.agents/skills/webapp-testing/scripts/with_server.py`. CI runs on every push: backend, frontend, browser, hygiene and evals, and the evals job fails if any safety invariant regresses. Live-model evaluation is never part of CI.

## Docker

A backend-only image (simulator-backed, synthetic data):

```powershell
docker build -t faultweld .
docker run --rm -p 127.0.0.1:8000:8000 faultweld
Invoke-RestMethod http://127.0.0.1:8000/health
```

The image installs from the hash-pinned `requirements.lock`, runs as a non-root user, contains no evaluator-only expectations, and serves only the normal app (not the demo app). Publish the port to `127.0.0.1` only. It uses the scripted model unless you pass `GROQ_API_KEY` yourself.

## Limitations

- **Live results describe a sample.** One model, one prompt version, this attack corpus; they are not a general statement about the model.
- **The scripted suite does not measure variance.** Its repeats measure determinism. Variance is the live evaluation's job.
- **The cluster is simulated.** It mirrors the sample deployment's fixtures, not Kubernetes in general.
- **The attack corpus is small and synthetic.** It measures compliance with these strings and the controls around them, not resistance to injection in general.
- **The live harness approves everything policy allows**, so it does not measure reviewer behavior.
- **The wall-clock budget is not enforced in evaluation** (a fixed clock keeps requests reproducible); per-call timeouts and token and cost caps still apply.
- **The model sees only four read tools over simulated state.** Behavior on real clusters, real logs or other tools is not covered.
- **Third-party processing.** Live runs send redacted synthetic evidence to Groq (or OpenRouter). Do not point it at real data.
- **The stale-guidance fixture** uses synthetic evidence, not real runbook retrieval.
- **Single-process SQLite.** Concurrency is tested with separate connections to one file, not across machines.
- **Restart reconciliation can be ambiguous.** If a lost response cannot be told apart from a concurrent change, the attempt stays `execution_unknown` and escalates instead of retrying.
- **Local demo identity.** The approver is a fixed local user, not an identity provider.
- This is a portfolio prototype. It makes no claim of being secure, production-ready or prompt-injection-proof.

## Real-cluster mode (optional)

The original design ran against a disposable `kind` cluster. That tooling is still in the repo under `infra/` and `scripts/` (`cluster.py`, `scenario.py`, `evidence.py`, `telemetry.py`): a read-only Kubernetes evidence adapter, a scoped RBAC setup, and an OpenTelemetry/Prometheus/Grafana stack. It is not used by the default workflow, the demos, or the gateway, which only ever writes to the simulator. It needs Docker, `kind`, `kubectl` and `helm`.