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

The single documented developer entrypoint starts both projects:

```powershell
python scripts\dev.py
```

The backend is available at `http://127.0.0.1:8000`, including `GET /health`, and the frontend is available at `http://127.0.0.1:5173`. Stop the entrypoint with `Ctrl+C`.

The projects can also be built independently:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --app-dir backend
npm --prefix frontend run build
```

## Checks

```powershell
.\.venv\Scripts\python -m ruff check backend
.\.venv\Scripts\python -m mypy backend
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run build
python scripts\check_hygiene.py
```

To regenerate the backend lockfile after an intentional dependency update:

```powershell
python -m pip install pip-tools==7.6.1
python -m piptools compile --generate-hashes --allow-unsafe --strip-extras --output-file backend\requirements.lock backend\requirements.in
```

## Environment

`.env.example` contains placeholders only. The scaffold does not require credentials, model providers, a database, or a Kubernetes cluster. Local `.env` files and kubeconfig files are ignored and must not be committed.

The uncurated root `skills/` directory, if present, is ignored and is not part of the application scaffold. Curated files under `.agents/skills/` are preserved.
