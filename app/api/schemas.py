"""Request and response models — SPEC.md §5.

Separate from `app/domain/job.py` on purpose. The domain model carries fields
the wire should not (`failure.detail` is logged, never returned), and pinning
the HTTP shape here means a domain refactor cannot silently change the API
contract.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.job import ArtifactRef, CostBreakdown, Job

MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class CreateVideoRequest(BaseModel):
    query: str = Field(..., description="A chemistry question in plain English.")


class PublicFailure(BaseModel):
    """`detail` is deliberately absent — it is internal (SPEC.md §3)."""

    code: str
    stage: str
    message: str


class JobAccepted(BaseModel):
    job_id: str
    status: str
    stage: str | None
    concept: str | None
    query: str
    created_at: datetime

    @classmethod
    def of(cls, job: Job) -> JobAccepted:
        return cls(
            job_id=job.job_id,
            status=job.status,
            stage=job.stage,
            concept=job.concept,
            query=job.query,
            created_at=job.created_at,
        )


class JobSummary(BaseModel):
    job_id: str
    query: str
    concept: str | None
    status: str
    stage: str | None
    degraded: bool
    created_at: datetime
    duration_s: float | None

    @classmethod
    def of(cls, job: Job) -> JobSummary:
        return cls(
            job_id=job.job_id,
            query=job.query,
            concept=job.concept,
            status=job.status,
            stage=job.stage,
            degraded=job.degraded,
            created_at=job.created_at,
            duration_s=job.artifact.duration_s if job.artifact else None,
        )


class JobListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobSummary]


class JobDetail(BaseModel):
    job_id: str
    query: str
    concept: str | None
    status: str
    stage: str | None
    degraded: bool
    attempts: int
    failure: PublicFailure | None
    artifact: ArtifactRef | None
    cost: CostBreakdown
    timings: dict[str, float]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, job: Job) -> JobDetail:
        return cls(
            job_id=job.job_id,
            query=job.query,
            concept=job.concept,
            status=job.status,
            stage=job.stage,
            degraded=job.degraded,
            attempts=job.attempts,
            failure=(
                PublicFailure(
                    code=job.failure.code,
                    stage=job.failure.stage,
                    message=job.failure.message,
                )
                if job.failure
                else None
            ),
            artifact=job.artifact,
            cost=job.cost,
            timings=job.timings,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


class ConceptInfo(BaseModel):
    """Makes the extension point visible to a reader of the API alone."""

    key: str
    canonical_question: str
    aliases: list[str]


class HealthResponse(BaseModel):
    status: str
    ffmpeg: bool
    queue_depth: int
