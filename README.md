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
the later scenario fixtures. Setup pulls a Linux amd64 copy of that image and
loads it into every kind node so scale-to-zero scenario resets do not depend on
a second node reaching the registry. Teardown refuses to run unless the active
kubeconfig context is exactly `kind-faultweld` and the namespace has the
`faultweld.dev/managed=true` label; it uninstalls the Helm release, then removes
the namespace and cluster.

## Telemetry

v0.3.1 adds a small metrics pipeline for the sample workload, defined in
`infra/kubernetes/telemetry.yaml` and applied by `scripts/cluster.py setup`
together with the rest of `infra/kubernetes`:

```
kube-state-metrics -> OpenTelemetry Collector -> Prometheus -> Grafana
```

- `kube-state-metrics` v2.15.0 reads Deployment and Pod state in `faultweld-demo`
  only, through a namespaced read-only Role (`list`, `watch`). There is no
  ClusterRole.
- OpenTelemetry Collector contrib 0.128.0 scrapes it every 5 seconds and exposes the
  result on port 8889. It also accepts OTLP on 4317/4318 for later versions. Series
  expire 20 seconds after their source stops reporting.
- Prometheus v3.5.0 keeps two hours of data in an `emptyDir`; Grafana 12.0.2 serves the
  provisioned, read-only dashboard "Faultweld sample service" to anonymous viewers.
  There is no admin account. Reach it only through a loopback port-forward.
- This stack is separate from the OpenTelemetry Demo's own Prometheus and Grafana.

Telemetry never converts missing data into health. If the collector, the scrape target,
or the replica series is down, absent, or older than 30 seconds, the reading is
`unavailable` or `stale` and carries no health value; dashboard panels read `no data`.

```powershell
# Dashboard (keep this terminal open), then browse to the URL below
kubectl -n faultweld-demo port-forward --address 127.0.0.1 svc/faultweld-grafana 3000:3000
# http://127.0.0.1:3000/d/faultweld-sample

# Current telemetry reading, and the live check against the disposable cluster
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python scripts\telemetry.py status
.\.venv\Scripts\python scripts\telemetry.py verify
```

`verify` resets and injects `healthy-replica-loss` and `readiness-regression`, waits
(bounded) for telemetry to report healthy, then faulted, then healthy, and records the lag.
It then scales `faultweld-kube-state-metrics` to zero, requires the outage to be detected
and not read as healthy, restores it, and requires recovery. `deployment-unavailable` is
reported as not asserted: its pod is replaced faster than a 5-second scrape reliably
observes, so use `scripts\scenario.py health` immediately after `start` for that fixture.

Resource budget (container limits and requests set in the manifest, not measured usage):

| Component | Memory request / limit | CPU request |
|---|---|---|
| kube-state-metrics | 64Mi / 128Mi | 20m |
| OpenTelemetry Collector | 128Mi / 256Mi | 50m |
| Prometheus | 256Mi / 512Mi | 100m |
| Grafana | 128Mi / 256Mi | 50m |

The four pods add about 576Mi of memory requests and 1152Mi of limits on top of the
demo. Measure real usage with `kubectl -n faultweld-demo top pods` only if metrics-server
is installed; it is not part of this setup.

To remove only the telemetry stack while keeping the rest of the cluster:

```powershell
kubectl config current-context   # must print kind-faultweld
kubectl -n faultweld-demo delete -f infra\kubernetes\telemetry.yaml
```

Full cleanup remains `scripts\cluster.py teardown`, which removes the namespace.

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

Type-check the cluster, scenario, and telemetry scripts from `backend` so mypy resolves the backend package imports:

```powershell
Push-Location backend
..\.venv\Scripts\python -m mypy --config-file pyproject.toml ..\scripts\cluster.py ..\scripts\scenario.py ..\scripts\telemetry.py
Pop-Location
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
