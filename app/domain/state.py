"""Job state machine — SPEC.md §4.

The transition table and its guard. Every rejection raises `InvalidTransition`
rather than returning a bool, because a caller that ignores a bool leaves the
job in a state nobody checked; an exception cannot be ignored by accident.

The five rules from SPEC.md §4:

1. `completed` requires a non-null artifact.
2. `failed` requires a non-null failure.
3. Terminal states never transition again.
4. A job may not stay in `running` without a stage.
5. `degraded=true` is compatible with `completed`.

Rules 1 and 2 are enforced *here* rather than left to the pipeline, so that
"completed with no artifact" is unrepresentable no matter which caller is
buggy. That is the structural half of the guarantee whose runtime half is G7.
"""

from __future__ import annotations

from app.domain.job import ArtifactRef, Failure, Job, JobStage, JobStatus, utcnow

STAGE_ORDER: tuple[JobStage, ...] = (
    "resolving",
    "scripting",
    "narrating",
    "rendering",
    "muxing",
    "publishing",
)

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    "queued": frozenset({"running", "failed"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}

TERMINAL: frozenset[JobStatus] = frozenset({"completed", "failed"})


class InvalidTransition(Exception):
    """An illegal move. Logged, never silently ignored (SPEC.md §4 rule 3)."""


def _reject(job: Job, target: str, reason: str) -> None:
    raise InvalidTransition(
        f"job {job.job_id}: {job.status}/{job.stage} -> {target} rejected: {reason}"
    )


def transition(
    job: Job,
    to: JobStatus,
    *,
    stage: JobStage | None = None,
    artifact: ArtifactRef | None = None,
    failure: Failure | None = None,
) -> Job:
    """Move `job` to `to`, or raise.

    Every check runs before any mutation, so a rejected transition leaves the
    job exactly as it was — a half-applied transition would be worse than no
    guard at all.
    """
    allowed = ALLOWED_TRANSITIONS[job.status]

    if to not in allowed:
        if job.status in TERMINAL:
            _reject(job, to, f"{job.status} is terminal")
        _reject(job, to, f"only {sorted(allowed)} permitted from {job.status}")

    if to == "running" and stage is None and job.stage is None:
        _reject(job, to, "running requires a stage (rule 4)")

    if to == "completed" and artifact is None:
        _reject(job, to, "completed requires an artifact (rule 1)")

    if to == "failed" and failure is None:
        _reject(job, to, "failed requires a named failure (rule 2)")

    job.status = to
    if stage is not None:
        job.stage = stage
    if artifact is not None:
        job.artifact = artifact
    if failure is not None:
        job.failure = failure
    job.updated_at = utcnow()
    return job


def advance_stage(job: Job, to: JobStage) -> Job:
    """Move the stage marker forward within `running`.

    Forward-only: stages describe irreversible progress through the pipeline,
    so a backwards move means a caller lost track of where it was, and
    silently allowing it would corrupt the timings map.
    """
    if job.status in TERMINAL:
        _reject(job, to, f"{job.status} is terminal")

    if job.stage is None:
        _reject(job, to, "job has no current stage")

    if STAGE_ORDER.index(to) <= STAGE_ORDER.index(job.stage):
        _reject(job, to, f"stages advance only forward, from {job.stage}")

    job.stage = to
    job.updated_at = utcnow()
    return job
