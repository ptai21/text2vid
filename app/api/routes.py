"""HTTP surface — SPEC.md §5.

This layer holds no business logic (CLAUDE.md §8): validate, resolve, hand to
the pipeline, map the result to HTTP. Anything that reasons about chemistry,
gates or artifacts belongs one layer down.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.errors import APIError, not_found
from app.api.schemas import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ConceptInfo,
    CreateVideoRequest,
    HealthResponse,
    JobAccepted,
    JobDetail,
    JobListResponse,
    JobSummary,
)
from app.concepts.aliases import resolve
from app.concepts.registry import all_concepts
from app.domain.job import Job
from app.logging import get_logger
from app.pipeline.runner import JobRunner
from app.providers import ffmpeg
from app.storage.artifacts import MANIFEST_NAME, VIDEO_NAME, ArtifactStore
from app.storage.repository import JobRepository

log = get_logger(__name__)

router = APIRouter()


def get_repository(request: Request) -> JobRepository:
    return request.app.state.repository


def get_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


async def _load(repository: JobRepository, job_id: str) -> Job:
    job = await repository.get(job_id)
    if job is None:
        raise not_found(job_id)
    return job


def _require_bundle_member(job: Job, store: ArtifactStore, name: str) -> None:
    """409 rather than 404 while a job is still working.

    SPEC.md §5: the envelope carries the current status and stage, so a
    polling client learns to wait instead of concluding the job never existed.
    """
    if job.status != "completed" or not store.exists(job.job_id, name):
        raise APIError(
            status.HTTP_409_CONFLICT,
            "not_ready",
            "The job has not produced this artifact yet.",
            status=job.status,
            stage=job.stage,
        )


@router.post("/videos", status_code=status.HTTP_202_ACCEPTED,
             response_model=JobAccepted, tags=["videos"])
async def create_video(
    payload: CreateVideoRequest,
    repository: JobRepository = Depends(get_repository),
    runner: JobRunner = Depends(get_runner),
) -> JobAccepted:
    """Returns immediately with a job id; generation runs in the background.

    `resolve` raises `ResolutionError` for an unsupported, ambiguous or
    malformed query, which the handler turns into a 400 — before any spend.
    """
    concept = resolve(payload.query)

    job = Job.create(query=payload.query)
    job.concept = concept
    await repository.create(job)

    runner.submit(job)
    log.info("job.accepted", job_id=job.job_id, concept=concept)

    return JobAccepted.of(job)


@router.get("/videos", response_model=JobListResponse, tags=["videos"])
async def list_videos(
    status_filter: str | None = Query(default=None, alias="status"),
    concept: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    repository: JobRepository = Depends(get_repository),
) -> JobListResponse:
    jobs, total = await repository.list(
        status=status_filter, concept=concept, limit=limit, offset=offset
    )
    return JobListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[JobSummary.of(job) for job in jobs],
    )


@router.get("/videos/{job_id}", response_model=JobDetail, tags=["videos"])
async def get_video(
    job_id: str,
    repository: JobRepository = Depends(get_repository),
) -> JobDetail:
    return JobDetail.of(await _load(repository, job_id))


@router.get("/videos/{job_id}/artifact", tags=["videos"])
async def get_artifact(
    job_id: str,
    repository: JobRepository = Depends(get_repository),
    store: ArtifactStore = Depends(get_store),
) -> Response:
    job = await _load(repository, job_id)
    _require_bundle_member(job, store, VIDEO_NAME)

    headers = {
        "Content-Disposition": f'inline; filename="{job_id}.mp4"',
    }
    if job.artifact is not None:
        headers["Content-Length"] = str(job.artifact.size_bytes)

    return StreamingResponse(
        store.open(job_id, VIDEO_NAME), media_type="video/mp4", headers=headers
    )


@router.get("/videos/{job_id}/manifest", tags=["videos"])
async def get_manifest(
    job_id: str,
    repository: JobRepository = Depends(get_repository),
    store: ArtifactStore = Depends(get_store),
) -> Response:
    """The run record — the observability surface (SPEC.md §12)."""
    job = await _load(repository, job_id)
    _require_bundle_member(job, store, MANIFEST_NAME)

    with store.open(job_id, MANIFEST_NAME) as handle:
        return Response(content=handle.read(), media_type="application/json")


@router.get("/concepts", response_model=list[ConceptInfo], tags=["concepts"])
async def list_concepts() -> list[ConceptInfo]:
    """Adding a fourth STEM topic is one registry entry, and this endpoint is
    where that shows up without reading the source."""
    return [
        ConceptInfo(
            key=concept.key,
            canonical_question=concept.canonical_question,
            # An alias is a tuple of terms that must all be present; joining
            # with " + " keeps that readable instead of hiding the conjunction.
            aliases=[" + ".join(alias) for alias in concept.aliases],
        )
        for concept in all_concepts()
    ]


@router.get("/health", response_model=HealthResponse, tags=["ops"])
async def health(runner: JobRunner = Depends(get_runner)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        ffmpeg=ffmpeg.available(),
        queue_depth=runner.queue_depth,
    )
