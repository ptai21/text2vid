"""Application assembly and wiring.

The only place the concrete implementations are chosen. Everything downstream
depends on the Protocols in `app/storage/` and the generator signature in
`app/pipeline/runner.py`, so swapping the in-memory repository for a database
or the stub generator for the real orchestrator is a change here and nowhere
else — which is what makes R9's "obvious where a real provider plugs in"
demonstrable rather than asserted.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import routes
from app.api.errors import register_handlers
from app.config import Settings, get_settings
from app.logging import configure_logging, get_logger
from app.pipeline.runner import JobRunner
from app.pipeline.stub import StubGenerator
from app.providers import ffmpeg
from app.storage.artifacts import LocalArtifactStore
from app.storage.repository import InMemoryJobRepository

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)

    repository = InMemoryJobRepository()
    artifact_store = LocalArtifactStore(settings.artifact_dir)

    # Round 6 swaps StubGenerator for the real orchestrator. Nothing else in
    # the application needs to change when it does.
    generate = StubGenerator(artifact_store, settings)

    runner = JobRunner(
        repository,
        generate,
        max_concurrent=settings.max_concurrent_jobs,
        timeout_s=settings.job_timeout_s,
    )

    app.state.repository = repository
    app.state.artifact_store = artifact_store
    app.state.runner = runner

    log.info(
        "startup",
        model=settings.gemini_model,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        artifact_dir=settings.artifact_dir.as_posix(),
        ffmpeg=ffmpeg.available(),
    )
    yield

    await runner.drain()
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

    register_handlers(app)
    app.include_router(router=routes.router)
    return app


app = create_app()
