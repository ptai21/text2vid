"""Application configuration.

Every tunable comes from `.env` through pydantic-settings. Nothing in the
codebase reads `os.environ` directly (CLAUDE.md §6) — that rule is what keeps
the settings surface enumerable and lets tests override it in one place.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Defaults mirror .env.example so the app boots without a .env; only the
    # API key genuinely has no sensible default.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    tts_voice: str = "en-US-AriaNeural"
    tts_rate: str = "+0%"

    max_concurrent_jobs: int = 2
    max_script_attempts: int = 3
    job_timeout_s: int = 600

    artifact_dir: Path = Path("./artifacts")

    video_width: int = 1280
    video_height: int = 720
    video_fps: int = 30

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Cached so the `.env` file is read once per process."""
    return Settings()
