"""Application assembly and wiring.

Round 1 keeps `/health` here because `app/api/` does not exist yet; round 4
introduces `app/api/routes.py` and the health route moves behind it with the
rest of the HTTP surface.
"""

import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


def ffmpeg_available() -> bool:
    """Both binaries, not just ffmpeg.

    G5 and G7 depend on `ffprobe` specifically, and installing ffmpeg without
    ffprobe is a common and confusing failure (SETUP.md §1) — reporting
    `"ffmpeg": true` on a box that cannot probe would hide it until muxing.

    This is a PATH lookup rather than an ffmpeg invocation, so it does not
    breach the provider boundary in CLAUDE.md §8. Round 7 creates
    `app/providers/ffmpeg.py` and this delegates to it.
    """
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def queue_depth() -> int:
    """Jobs waiting on the runner's semaphore.

    Structurally zero until round 4 introduces the runner — reported rather
    than omitted so the health contract in SPEC.md §5 is stable from the start.
    """
    return 0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "startup",
        model=settings.gemini_model,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        artifact_dir=settings.artifact_dir.as_posix(),
        ffmpeg=ffmpeg_available(),
    )
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    # Configured here rather than in `lifespan` so that uvicorn's own startup
    # lines ("Started server process", "Waiting for application startup") are
    # already JSON. Configuring later leaves a short plain-text window at the
    # head of every run, which is exactly where boot failures show up.
    configure_logging(get_settings().log_level)

    app = FastAPI(
        title="text2vid",
        version="0.1.0",
        summary="Asynchronous chemistry explainer video generation.",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "ffmpeg": ffmpeg_available(),
            "queue_depth": queue_depth(),
        }

    return app


app = create_app()
