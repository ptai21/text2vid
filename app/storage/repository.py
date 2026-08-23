"""Job persistence — SPEC.md §12.

In-memory behind a Protocol. The brief permits this when the boundary is
clean, and the boundary is the point: swapping to Postgres is one new class,
not a refactor (CLAUDE.md §4).

Methods are `async` because `InMemoryJobRepository` guards writes with an
`asyncio.Lock` — two jobs run concurrently under the runner's semaphore, and
a real repository would do I/O here anyway. Keeping the signatures async now
means the swap never touches a call site.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.domain.job import Job


class JobRepository(Protocol):
    async def create(self, job: Job) -> None: ...

    async def get(self, job_id: str) -> Job | None: ...

    async def list(
        self,
        *,
        status: str | None = None,
        concept: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Job], int]: ...

    async def update(self, job: Job) -> None: ...


class InMemoryJobRepository:
    """Insertion-ordered; listing returns newest first.

    SPEC.md §12 does not fix an order, and newest-first is what a client
    polling a job list actually wants.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job

    async def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def list(
        self,
        *,
        status: str | None = None,
        concept: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Job], int]:
        jobs = list(self._jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        if concept is not None:
            jobs = [j for j in jobs if j.concept == concept]

        jobs.sort(key=lambda j: j.created_at, reverse=True)
        total = len(jobs)
        return jobs[offset: offset + limit], total

    async def update(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job
