# syntax=docker/dockerfile:1.7
# Faultweld Lite backend API (simulator-backed; synthetic data only).

# ---- builder: resolve dependencies from the hash-pinned lock file ----------------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY backend/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes -r /tmp/requirements.lock

# ---- runtime: no build tools, non-root, read-only-friendly ------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/backend \
    APP_ENV=production \
    BACKEND_PORT=8000
RUN groupadd --system faultweld && useradd --system --gid faultweld --no-create-home faultweld
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY backend/app ./backend/app
COPY scenarios/fixtures ./scenarios/fixtures
COPY scripts ./scripts
USER faultweld
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('BACKEND_PORT','8000'), timeout=2)"
# Bind all interfaces inside the container; publish the port to 127.0.0.1 on the host.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${BACKEND_PORT}"]
