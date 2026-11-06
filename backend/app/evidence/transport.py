"""Read-only Kubernetes transport. The only operation is an HTTP GET of an API path."""

from __future__ import annotations

import subprocess
from typing import Literal, Protocol

from app.evidence.redaction import redact_text

KIND_CONTEXT = "kind-faultweld"
AGENT_IDENTITY = "system:serviceaccount:faultweld-demo:faultweld-agent-readonly"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_ERROR_LENGTH = 200
ALLOWED_PATH_PREFIXES = ("/api/v1/", "/apis/apps/v1/")

TransportErrorCode = Literal["forbidden", "not_found", "unavailable"]


class TransportError(RuntimeError):
    def __init__(self, code: TransportErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class KubernetesTransport(Protocol):
    def get(self, path: str) -> str: ...


def classify_stderr(stderr: str) -> TransportErrorCode:
    lowered = stderr.lower()
    if "(forbidden)" in lowered:
        return "forbidden"
    if "(notfound)" in lowered:
        return "not_found"
    return "unavailable"


def summarize_stderr(stderr: str) -> str:
    lines = stderr.strip().splitlines()
    first = redact_text(lines[0]) if lines else "no error output"
    return first[:MAX_ERROR_LENGTH]


class KubectlRawTransport:
    """``kubectl get --raw`` pinned to one context and, by default, the agent identity.

    Impersonating the read-only service account makes the API server enforce the same
    RBAC the agent will have, so a forbidden read here is a forbidden read for the agent.
    """

    def __init__(
        self,
        *,
        context: str = KIND_CONTEXT,
        impersonate: str | None = AGENT_IDENTITY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        kubectl: str = "kubectl",
    ) -> None:
        self._context = context
        self._impersonate = impersonate
        self._timeout_seconds = timeout_seconds
        self._kubectl = kubectl

    def command(self, path: str) -> list[str]:
        if not path.startswith(ALLOWED_PATH_PREFIXES):
            raise ValueError(f"path is outside the readable API groups: {path!r}")
        command = [self._kubectl, "--context", self._context]
        if self._impersonate is not None:
            command += ["--as", self._impersonate]
        return [*command, "get", "--raw", path]

    def get(self, path: str) -> str:
        command = self.command(path)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise TransportError("unavailable", "kubectl timed out") from error
        except OSError as error:
            raise TransportError("unavailable", "kubectl could not be started") from error
        if result.returncode != 0:
            raise TransportError(classify_stderr(result.stderr), summarize_stderr(result.stderr))
        return result.stdout
