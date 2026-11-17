"""Application settings loaded from the project's .env file (python-dotenv)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)


@dataclass(frozen=True)
class Settings:
    """Typed settings. Real environment variables override values in .env."""

    model_name: str = os.getenv("MODEL_NAME", "amazon/chronos-bolt-small")
    api_key: str = os.getenv("API_KEY", "")
    context_hours: int = int(os.getenv("CONTEXT_HOURS", "672"))
    horizon_hours: int = int(os.getenv("HORIZON_HOURS", "144"))


settings = Settings()
