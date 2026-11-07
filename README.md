# Faultweld

Foundation scaffold for the local Faultweld project. This repository currently contains the independently buildable `backend/` and `frontend/` projects only; incident workflows, model calls, Kubernetes integration, and remediation behavior are intentionally not implemented yet.

## Prerequisites

- Python 3.12
- Node.js 24
- npm 11

## Setup

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r backend\requirements.lock
npm ci --prefix frontend
```

The checked-in lockfiles are the source for reproducible dependency installation. The frontend uses plain CSS and has no additional styling framework.

## Development

The single documented developer entrypoint starts both application projects:

```powershell
python scripts\dev.py
```

The backend is available at `http://127.0.0.1:8000`, including `GET /health`, and the frontend is available at `http://127.0.0.1:5173`. Stop the entrypoint with `Ctrl+C`.

The projects can also be built independently:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --app-dir backend
npm --prefix frontend run build
```

## Disposable cluster

v0.2.1 adds an optional local kind cluster for the OpenTelemetry Demo and a
small fault-injection target. Install Docker Desktop (using Linux containers),
`kind`, `kubectl`, and Helm 3 or newer, then run these commands from the
repository root:

```powershell
.\.venv\Scripts\python scripts\cluster.py setup
.\.venv\Scripts\python scripts\cluster.py reset
.\.venv\Scripts\python scripts\cluster.py teardown
```

Setup creates the pinned `faultweld` kind cluster, the managed `faultweld-demo`
namespace, and installs OpenTelemetry Demo Helm chart `0.42.0` (application
`3.1.0`). The [official chart guide](https://opentelemetry.io/docs/platforms/kubernetes/helm/demo/)
estimates around 4 GB of memory for its default installation; allow additional
memory for Docker and the kind nodes. First setup can take a while because the
demo pulls many container images. The setup makes the kind control-plane schedulable so
the demo can spread across both nodes. Its demo-only agent, chatbot, MCP, and
load-generator components are disabled to keep this milestone focused on
observable services. A pinned `hashicorp/http-echo:1.0.0`
deployment named `faultweld-sample` remains the small, deterministic target for
the later scenario fixtures. Teardown refuses to run unless the active
kubeconfig context is exactly `kind-faultweld` and the namespace has the
`faultweld.dev/managed=true` label; it uninstalls the Helm release, then removes
the namespace and cluster.

## Kubernetes evidence

v0.3.2 adds a read-only Kubernetes evidence adapter in `backend/app/evidence`. It turns
Deployments, Pods, and Services in `faultweld-demo` into versioned `Evidence` records with
a bounded, redacted payload, a content hash, and an observation time.

- Reads are allow-listed (`deployment`, `pod`, `service`) and always scoped to
  `faultweld-demo`; a query has no namespace field. Names and label selectors are validated,
  and only `k=v`, `k==v`, `k!=v`, `k`, and `!k` selectors are accepted.
- The live transport runs `kubectl --context kind-faultweld --as <agent identity> get --raw`,
  so the API server enforces the real `faultweld-agent-readonly` RBAC. That Role grants only
  `get`, `list`, and `watch` on pods, services, and deployments, and nothing on Secrets.
- Payloads are whitelist projections. Annotations, managed fields, container environment and
  arguments, and volumes are never copied. Free text is redacted for tokens and auth headers
  and truncated to 200 characters. Response size, per-item size, total size, and item count
  are bounded; a read over a bound fails or is flagged `truncated`, never silently shortened.
- A missing resource is reported as `not_found` with no evidence. A forbidden or unreachable
  API raises a coded error so callers can mark the source missing instead of guessing.
- Kubernetes omits zero-valued Deployment status counters, so absent counters are reported
  as `0`.

```powershell
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python scripts\evidence.py read deployment --name faultweld-sample
.\.venv\Scripts\python scripts\evidence.py read pod --selector app.kubernetes.io/name=faultweld-sample
.\.venv\Scripts\python scripts\evidence.py verify
```

`verify` is read-only. It checks that evidence is produced for the sample Deployment, Pods,
and Service; that a missing resource is reported as not found; that the agent identity cannot
read Secrets or other namespaces and cannot write; and that payloads contain no sensitive
fields.

## Faultweld Lite (simulated cluster)

The default environment is an in-process simulation of the sample deployment, so no Docker or
Kubernetes is needed. The kind-based setup above remains available as an optional real mode.

```powershell
.\.venv\Scripts\python scripts\sim.py run healthy-replica-loss
.\.venv\Scripts\python scripts\sim.py watch deployment-unavailable
.\.venv\Scripts\python scripts\sim.py verify
```

Time in the simulation is a tick counter, so a deleted pod is replaced after exactly three
ticks on every run.

## Checks

```powershell
.\.venv\Scripts\python -m ruff check backend frontend\tests scripts
.\.venv\Scripts\python -m mypy --config-file backend\pyproject.toml backend\app backend\tests
.\.venv\Scripts\python -m mypy --strict frontend\tests\browser\console_smoke.py
.\.venv\Scripts\python -m unittest discover -s backend\tests -t backend -p "test_*.py"
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run test
npm --prefix frontend run build
.\.venv\Scripts\python scripts\check_hygiene.py
```

The frontend has two test layers. `npm --prefix frontend run test` runs the deterministic component suite (Vitest with jsdom) against the real application shell. The browser smoke suite drives a real Chromium instance against the Vite dev server and needs the browser binary once per machine:

```powershell
.\.venv\Scripts\python -m playwright install chromium
python .agents\skills\webapp-testing\scripts\with_server.py --server "npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173" --port 5173 -- .\.venv\Scripts\python.exe frontend\tests\browser\console_smoke.py
```

On Windows the helper stops the `npm` wrapper but can leave the `vite` child process running, which then holds files under `frontend\node_modules`. Stop any leftover `node` process before running `npm ci` again.

Both layers stub every API response, so neither needs a model provider, a database, or a Kubernetes cluster, and the backend is not required to run them. In CI the component suite runs in the `Frontend checks` job and the browser suite in the separate `Browser smoke and accessibility checks` job, so frontend failures never mask or depend on backend results.

To regenerate the backend lockfile after an intentional dependency update:

```powershell
python -m pip install pip-tools==7.6.1
python -m piptools compile --generate-hashes --allow-unsafe --strip-extras --output-file backend\requirements.lock backend\requirements.in
```

## Environment

`.env.example` contains placeholders only. The scaffold does not require credentials, model providers, a database, or a Kubernetes cluster. Local `.env` files and kubeconfig files are ignored and must not be committed.

The uncurated root `skills/` directory, if present, is ignored and is not part of the application scaffold. Curated files under `.agents/skills/` are preserved.
