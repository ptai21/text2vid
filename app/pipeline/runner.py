"""Async job runner — background execution with a concurrency cap.

R2: submitting returns immediately with a job id and generation runs in the
background. Latency is explicitly not a concern; *clearly exposing the waiting
state* is, which is why stage changes are persisted as they happen rather than
only at the end.

Two guarantees this module exists to provide:

- **No job is ever abandoned in `running`.** Any exception escaping the task
  becomes `failed` + `internal_error`, and `JOB_TIMEOUT_S` catches the case
  where nothing raises at all but nothing finishes either.
- **Concurrency is capped.** A semaphore of `MAX_CONCURRENT_JOBS`, so a burst
  of submissions queues instead of launching unbounded ffmpeg processes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.domain.job import ArtifactRef, Failure, FailureCode, Job, JobStage
from app.domain.state import advance_stage, transition
from app.logging import get_logger
from app.storage.repository import JobRepository

log = get_logger(__name__)

StageReporter = Callable[[JobStage], Awaitable[None]]
Generator = Callable[[Job, StageReporter], Awaitable[ArtifactRef]]


class StageFailure(Exception):
    """A generator stage failing for a reason it can name.

    Part of the generator contract, which is why it lives beside it. R7
    asks that failures be named and explicit; the generator is the only
    layer that knows *which* stage stopped and *why*, so it says so here
    rather than letting every cause collapse into `internal_error`.

    The catch-all below stays exactly as it was. What it now means is
    narrower and more useful: an internal_error is a bug, not a
    foreseeable failure nobody bothered to name.
    """

    def __init__(self, code: FailureCode, stage: JobStage, message: str,
                 detail: str = ""):
        super().__init__(f"{code} at {stage}: {message}")
        self.code = code
        self.stage = stage
        self.message = message
        self.detail = detail


class JobRunner:
    def __init__(
        self,
        repository: JobRepository,
        generate: Generator,
        *,
        max_concurrent: int,
        timeout_s: float,
    ):
        self._repository = repository
        self._generate = generate
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._timeout_s = timeout_s
        self._waiting = 0
        self._tasks: set[asyncio.Task] = set()

    @property
    def queue_depth(self) -> int:
        """Jobs submitted but still waiting on the semaphore.

        Not "jobs not yet finished" — a running job is not queued, and
        conflating the two would make `/health` report load it is coping with
        as though it were backlog.
        """
        return self._waiting

    def submit(self, job: Job) -> asyncio.Task:
        task = asyncio.create_task(self._run(job))
        # Without a strong reference the event loop may garbage-collect a
        # running task mid-flight, which looks exactly like a job vanishing.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Await outstanding tasks. Used at shutdown and by tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _run(self, job: Job) -> None:
        self._waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1

        try:
            await self._execute(job)
        finally:
            self._semaphore.release()

    async def _execute(self, job: Job) -> None:
        try:
            transition(job, "running", stage="resolving")
            await self._repository.update(job)

            artifact = await asyncio.wait_for(
                self._generate(job, self._reporter(job)),
                timeout=self._timeout_s,
            )

            transition(job, "completed", artifact=artifact)
            await self._repository.update(job)
            log.info("job.completed", job_id=job.job_id, concept=job.concept,
                     degraded=job.degraded)

        except asyncio.TimeoutError:
            log.warning("job.timeout", job_id=job.job_id, timeout_s=self._timeout_s)
            await self._fail(
                job,
                "Generation exceeded the time budget.",
                f"exceeded JOB_TIMEOUT_S={self._timeout_s}",
            )

        except StageFailure as exc:
            # A foreseen failure that already knows its own name. Logged at
            # warning rather than exception: there is no bug here and no
            # traceback worth reading, only a stage that could not proceed.
            log.warning("job.failed", job_id=job.job_id, code=exc.code,
                        stage=exc.stage, detail=exc.detail)
            await self._fail(job, exc.message, exc.detail,
                             code=exc.code, stage=exc.stage)

        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            # CLAUDE.md §11 forbids swallowing an error without a named
            # FailureCode. This records one and logs the traceback; what it
            # must never do is let the job sit in `running` forever.
            log.exception("job.internal_error", job_id=job.job_id)
            await self._fail(job, "Generation failed unexpectedly.", repr(exc))

    def _reporter(self, job: Job) -> StageReporter:
        async def report(stage: JobStage) -> None:
            advance_stage(job, stage)
            await self._repository.update(job)

        return report

    async def _fail(self, job: Job, message: str, detail: str, *,
                    code: FailureCode = "internal_error",
                    stage: JobStage | None = None) -> None:
        failure = Failure(
            code=code,
            stage=stage or job.stage or "resolving",
            message=message,
            detail=detail,
        )
        try:
            transition(job, "failed", failure=failure)
        except Exception:
            # Already terminal. Nothing left to record, and raising here would
            # replace a known failure with an unknown one.
            log.warning("job.already_terminal", job_id=job.job_id, status=job.status)
            return
        await self._repository.update(job)
