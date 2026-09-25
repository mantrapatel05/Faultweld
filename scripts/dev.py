from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    backend_host = os.environ.get("BACKEND_HOST", "127.0.0.1")
    backend_port = os.environ.get("BACKEND_PORT", "8000")
    frontend_port = os.environ.get("FRONTEND_PORT", "5173")
    npm_command = "npm.cmd" if os.name == "nt" else "npm"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                backend_host,
                "--port",
                backend_port,
            ],
            cwd=ROOT / "backend",
        ),
        subprocess.Popen(
            [npm_command, "run", "dev", "--", "--host", backend_host, "--port", frontend_port],
            cwd=ROOT / "frontend",
        ),
    ]

    try:
        while True:
            for process in processes:
                if process.poll() is not None:
                    return process.returncode or 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
