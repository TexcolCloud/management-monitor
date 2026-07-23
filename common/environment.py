"""Environment-file loading for command composition roots."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


def load_environment(path: Path | str | None = None) -> bool:
    """Load local settings once without overriding process-level configuration."""
    env_file = Path(path).resolve() if path else DEFAULT_ENV_FILE
    return bool(load_dotenv(dotenv_path=env_file, override=False))

