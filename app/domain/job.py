"""Job aggregate and its value objects — SPEC.md §3.

Pure data. This module imports nothing from `api/`, `pipeline/`, `providers/`
or `storage/` (CLAUDE.md §8), which is what lets every other layer depend on
it without creating a cycle.

`CostBreakdown` lives here rather than in `pipeline/cost.py` for that reason:
`Job` carries one, and `domain` may not import `pipeline`. The pipeline owns
the *arithmetic*; the domain owns the *shape*.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

ConceptKey = Literal["ph_scale", "covalent_bonds", "ionic_vs_covalent"]

JobStatus = Literal["queued", "running", "completed", "failed"]

JobStage = Literal[
    "resolving", "scripting", "narrating", "rendering", "muxing", "publishing"
]

FailureCode = Literal[
    "invalid_request",
    "unsupported_concept",
    "ambiguous_query",
    "script_unavailable",
    "tts_failed",
    "render_failed",
    "mux_failed",
    "artifact_invalid",
    "timeout",
    "internal_error",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Failure(BaseModel):
    """A named failure. R7: never silent, never half-completed."""

    code: FailureCode
    stage: JobStage
    message: str
    """Human-readable and safe to return to a client."""
    detail: str | None = None
    """Internal — logged, never returned (SPEC.md §5 error envelope)."""


class ArtifactRef(BaseModel):
    url: str
    duration_s: float
    size_bytes: int
    scenes: int


class CostBreakdown(BaseModel):
    """What the job actually spent, plus what it would cost in production.

    `tts_usd` is 0.0 because edge-tts is free; `production_estimate_usd`
    answers the R10 question without pretending the prototype paid for it.
    """

    llm_usd: float = 0.0
    tts_usd: float = 0.0
    total_usd: float = 0.0
    production_estimate_usd: float = 0.0


class Job(BaseModel):
    job_id: str
    query: str
    """The learner's raw text, verbatim.

    Never normalised and never interpolated into a prompt (CLAUDE.md §4). The
    resolver normalises a copy; the title card renders this.
    """

    concept: ConceptKey | None = None
    status: JobStatus = "queued"
    stage: JobStage | None = None

    degraded: bool = False
    """A fallback script was used. A quality flag, not a failure."""

    attempts: int = 0
    """LLM attempts consumed, retries included."""

    failure: Failure | None = None
    artifact: ArtifactRef | None = None
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    timings: dict[str, float] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def create(cls, query: str) -> Job:
        return cls(job_id=str(uuid.uuid4()), query=query)
