import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ENV_VALUES = {
    "APP_ENV": "development",
    "BACKEND_HOST": "127.0.0.1",
    "BACKEND_PORT": "8000",
    "FRONTEND_PORT": "5173",
    "VITE_API_URL": "http://127.0.0.1:8000",
    "OPENROUTER_API_KEY": "",
    "GROQ_API_KEY": "",
}


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    env_example = ROOT / ".env.example"
    if not env_example.is_file():
        fail(".env.example is missing")
    if (ROOT / "kubeconfig").exists():
        fail("kubeconfig must remain untracked")
    ignore_lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    if "/skills/" not in ignore_lines:
        fail("the uncurated root skills/ directory must be ignored")
    if ".env" not in ignore_lines:
        fail(".env must be ignored while local secrets are used")
    tracked_env = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if tracked_env.returncode == 0:
        fail(".env must remain untracked")
    if not (ROOT / ".agents" / "skills").is_dir():
        fail("curated .agents/skills is missing")

    values: dict[str, str] = {}
    for raw_line in env_example.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in values:
            fail(f"invalid .env.example line: {raw_line}")
        values[key] = value

    if values != EXPECTED_ENV_VALUES:
        fail(".env.example contains non-placeholder values")


if __name__ == "__main__":
    main()
