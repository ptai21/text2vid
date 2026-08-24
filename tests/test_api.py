"""API and async runner — SPEC.md §5 and §12, PLAN.md round 4.

T2: fake providers, no network, no ffmpeg. The generator is injected, so
these tests exercise the job machinery rather than the thing being generated
— which is the whole point of separating the lifecycle from the AI. One
`@pytest.mark.slow` test at the bottom checks the real `StubGenerator`
against ffmpeg; it is excluded from the fast suite by `addopts`.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import routes
from app.api.errors import register_handlers
from app.config import Settings
from app.domain.job import ArtifactRef
from app.pipeline.runner import JobRunner
from app.storage.artifacts import (
    MANIFEST_NAME,
    SCRIPT_NAME,
    VIDEO_NAME,
    LocalArtifactStore,
)
from app.storage.repository import InMemoryJobRepository

PH_QUERY = "How does the pH scale work?"
COVALENT_QUERY = "Why do atoms form covalent bonds?"
FAKE_VIDEO_BYTES = 2048


def _write_bundle(store: LocalArtifactStore, job_id: str) -> int:
    """Stand in for a produced bundle without invoking ffmpeg."""
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(b"\x00" * FAKE_VIDEO_BYTES)
        temp = Path(handle.name)
    try:
        store.put(job_id, VIDEO_NAME, temp)
        temp.write_text(json.dumps({"concept": "ph_scale", "scenes": []}),
                        encoding="utf-8")
        store.put(job_id, SCRIPT_NAME, temp)
        temp.write_text(json.dumps({"job_id": job_id, "gates": []}), encoding="utf-8")
        store.put(job_id, MANIFEST_NAME, temp)
    finally:
        temp.unlink(missing_ok=True)
    return FAKE_VIDEO_BYTES


# --- generator factories: each takes the store and returns a generator ------

def completing(gate: asyncio.Event | None = None):
    def factory(store: LocalArtifactStore):
        async def generate(job, report_stage) -> ArtifactRef:
            await report_stage("scripting")
            if gate is not None:
                await gate.wait()
            await report_stage("publishing")
            size = _write_bundle(store, job.job_id)
            return ArtifactRef(
                url=f"/videos/{job.job_id}/artifact",
                duration_s=68.4,
                size_bytes=size,
                scenes=5,
            )

        return generate

    return factory


def exploding():
    def factory(store):
        async def generate(job, report_stage):
            await report_stage("rendering")
            raise RuntimeError("matplotlib fell over")

        return generate

    return factory


def hanging():
    def factory(store):
        async def generate(job, report_stage):
            await report_stage("narrating")
            await asyncio.sleep(3600)

        return generate

    return factory


class Harness:
    def __init__(self, app, repository, store, runner):
        self.app = app
        self.repository = repository
        self.store = store
        self.runner = runner

    def client(self) -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test"
        )


@pytest.fixture
def build(tmp_path):
    def _build(factory, *, max_concurrent=2, timeout_s=60.0) -> Harness:
        app = FastAPI()
        register_handlers(app)
        app.include_router(routes.router)

        repository = InMemoryJobRepository()
        store = LocalArtifactStore(tmp_path / "artifacts")
        runner = JobRunner(
            repository,
            factory(store),
            max_concurrent=max_concurrent,
            timeout_s=timeout_s,
        )

        app.state.repository = repository
        app.state.artifact_store = store
        app.state.runner = runner
        return Harness(app, repository, store, runner)

    return _build


async def submit(client, query=PH_QUERY):
    return await client.post("/videos", json={"query": query})


# ---------------------------------------------------------------------------
# POST /videos
# ---------------------------------------------------------------------------

async def test_submitting_returns_202_with_a_job_id(build):
    harness = build(completing())
    async with harness.client() as client:
        response = await submit(client)
        await harness.runner.drain()

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert body["status"] == "queued"
    assert body["concept"] == "ph_scale"
    assert body["query"] == PH_QUERY


async def test_submission_returns_before_generation_finishes(build):
    """R2: generation runs in the background; the POST does not wait for it."""
    gate = asyncio.Event()
    harness = build(completing(gate))

    async with harness.client() as client:
        response = await submit(client)
        assert response.status_code == 202
        assert response.json()["status"] == "queued"

        gate.set()
        await harness.runner.drain()
        detail = (await client.get(f"/videos/{response.json()['job_id']}")).json()

    assert detail["status"] == "completed"


@pytest.mark.parametrize("query,code", [
    ("What is photosynthesis?", "unsupported_concept"),
    ("how does ph relate to ionic and covalent bonding", "ambiguous_query"),
    ("ph", "invalid_request"),
])
async def test_a_rejected_query_never_creates_a_job(build, query, code):
    harness = build(completing())
    async with harness.client() as client:
        response = await submit(client, query)
        listing = await client.get("/videos")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert listing.json()["total"] == 0, "rejection must happen before any spend"


async def test_an_unsupported_query_is_told_what_is_supported(build):
    harness = build(completing())
    async with harness.client() as client:
        response = await submit(client, "What is photosynthesis?")

    assert set(response.json()["error"]["supported_concepts"]) == {
        "ph_scale", "covalent_bonds", "ionic_vs_covalent",
    }


async def test_a_malformed_body_uses_the_same_error_envelope(build):
    harness = build(completing())
    async with harness.client() as client:
        response = await client.post("/videos", json={"nope": 1})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# GET /videos and GET /videos/{job_id}
# ---------------------------------------------------------------------------

async def test_listing_and_detail_reflect_a_completed_job(build):
    harness = build(completing())
    async with harness.client() as client:
        job_id = (await submit(client)).json()["job_id"]
        await harness.runner.drain()

        listing = (await client.get("/videos")).json()
        detail = (await client.get(f"/videos/{job_id}")).json()

    assert listing["total"] == 1
    assert listing["limit"] == 20 and listing["offset"] == 0
    summary = listing["items"][0]
    assert summary["job_id"] == job_id
    assert summary["status"] == "completed"
    assert summary["duration_s"] == 68.4

    assert detail["status"] == "completed"
    assert detail["artifact"]["scenes"] == 5
    assert detail["failure"] is None


async def test_listing_filters_by_status_and_concept(build):
    harness = build(completing())
    async with harness.client() as client:
        await submit(client, PH_QUERY)
        await submit(client, COVALENT_QUERY)
        await harness.runner.drain()

        by_concept = (await client.get("/videos?concept=ph_scale")).json()
        by_status = (await client.get("/videos?status=failed")).json()

    assert by_concept["total"] == 1
    assert by_concept["items"][0]["concept"] == "ph_scale"
    assert by_status["total"] == 0


async def test_listing_paginates(build):
    harness = build(completing())
    async with harness.client() as client:
        for _ in range(3):
            await submit(client)
        await harness.runner.drain()

        page = (await client.get("/videos?limit=2&offset=1")).json()

    assert page["total"] == 3
    assert page["limit"] == 2
    assert len(page["items"]) == 2


async def test_limit_above_the_maximum_is_rejected(build):
    harness = build(completing())
    async with harness.client() as client:
        response = await client.get("/videos?limit=500")
    assert response.status_code == 400


async def test_an_unknown_job_is_404_with_the_error_envelope(build):
    harness = build(completing())
    async with harness.client() as client:
        response = await client.get("/videos/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_failure_detail_is_never_returned_to_a_client(build):
    """SPEC.md §3: `detail` is internal, logged not returned."""
    harness = build(exploding())
    async with harness.client() as client:
        job_id = (await submit(client)).json()["job_id"]
        await harness.runner.drain()
        detail = (await client.get(f"/videos/{job_id}")).json()

    assert detail["failure"]["code"] == "internal_error"
    assert "detail" not in detail["failure"]
    assert "matplotlib fell over" not in json.dumps(detail)


# ---------------------------------------------------------------------------
# GET /videos/{job_id}/artifact
# ---------------------------------------------------------------------------

async def test_the_artifact_is_409_while_the_job_is_still_running(build):
    gate = asyncio.Event()
    harness = build(completing(gate))

    async with harness.client() as client:
        job_id = (await submit(client)).json()["job_id"]
        for _ in range(10):  # let the task reach the gate
            await asyncio.sleep(0)
        response = await client.get(f"/videos/{job_id}/artifact")

        gate.set()
        await harness.runner.drain()

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "not_ready"
    assert error["status"] == "running", "a polling client needs to know to wait"


async def test_the_artifact_is_200_once_the_job_completes(build):
    harness = build(completing())
    async with harness.client() as client:
        job_id = (await submit(client)).json()["job_id"]
        await harness.runner.drain()
        response = await client.get(f"/videos/{job_id}/artifact")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert job_id in response.headers["content-disposition"]
    assert len(response.content) == FAKE_VIDEO_BYTES


async def test_the_artifact_of_an_unknown_job_is_404(build):
    harness = build(completing())
    async with harness.client() as client:
        response = await client.get("/videos/nope/artifact")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /videos/{job_id}/manifest, GET /concepts, GET /health
# ---------------------------------------------------------------------------

async def test_the_manifest_is_served_once_the_job_completes(build):
    harness = build(completing())
    async with harness.client() as client:
        job_id = (await submit(client)).json()["job_id"]
        await harness.runner.drain()
        response = await client.get(f"/videos/{job_id}/manifest")

    assert response.status_code == 200
    assert response.json()["job_id"] == job_id


async def test_the_manifest_is_409_before_the_job_completes(build):
    gate = asyncio.Event()
    harness = build(completing(gate))
    async with harness.client() as client:
        job_id = (await submit(client)).json()["job_id"]
        response = await client.get(f"/videos/{job_id}/manifest")
        gate.set()
        await harness.runner.drain()

    assert response.status_code == 409


async def test_the_concepts_endpoint_exposes_the_registry(build):
    harness = build(completing())
    async with harness.client() as client:
        body = (await client.get("/concepts")).json()

    assert {c["key"] for c in body} == {
        "ph_scale", "covalent_bonds", "ionic_vs_covalent",
    }
    for concept in body:
        assert concept["canonical_question"]
        assert concept["aliases"]


async def test_health_reports_ffmpeg_and_queue_depth(build):
    harness = build(completing())
    async with harness.client() as client:
        body = (await client.get("/health")).json()

    assert body["status"] == "ok"
    assert isinstance(body["ffmpeg"], bool)
    assert body["queue_depth"] == 0


# ---------------------------------------------------------------------------
# The runner's two guarantees
# ---------------------------------------------------------------------------

async def test_an_exception_escaping_the_task_fails_the_job(build):
    """The job must never be abandoned in `running` (CLAUDE.md §11)."""
    harness = build(exploding())

    async with harness.client() as client:
        job_id = (await submit(client)).json()["job_id"]
        await harness.runner.drain()
        detail = (await client.get(f"/videos/{job_id}")).json()

    assert detail["status"] == "failed"
    assert detail["failure"]["code"] == "internal_error"
    assert detail["failure"]["stage"] == "rendering"


async def test_job_timeout_is_enforced(build):
    """Asserted `internal_error` until round 10, which was never a decision.

    Round 4 wrote this line because `FailureCode` had no `timeout` member yet,
    so the runner had nothing else to report. SPEC.md section 3 has always
    defined `internal_error` as *unhandled* - and a timeout is handled, in a
    named `except` block - so the old assertion recorded an accident rather
    than a contract. Changing it followed a spec change, not a red test.
    """
    harness = build(hanging(), timeout_s=0.05)

    async with harness.client() as client:
        job_id = (await submit(client)).json()["job_id"]
        await harness.runner.drain()
        detail = (await client.get(f"/videos/{job_id}")).json()

    assert detail["status"] == "failed"
    assert detail["failure"]["code"] == "timeout"


async def test_a_timed_out_job_is_never_left_running(build):
    harness = build(hanging(), timeout_s=0.05)
    async with harness.client() as client:
        await submit(client)
        await harness.runner.drain()
        listing = (await client.get("/videos?status=running")).json()

    assert listing["total"] == 0


async def test_the_semaphore_caps_concurrency_at_two(build):
    live = 0
    peak = 0

    def factory(store):
        async def generate(job, report_stage) -> ArtifactRef:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                await report_stage("rendering")
                await asyncio.sleep(0.02)
            finally:
                live -= 1
            return ArtifactRef(url="/x", duration_s=1.0, size_bytes=1, scenes=5)

        return generate

    harness = build(factory, max_concurrent=2)
    async with harness.client() as client:
        for _ in range(6):
            await submit(client)
        await harness.runner.drain()

    assert peak <= 2, f"{peak} generators ran at once; the cap is 2"


async def test_every_submitted_job_reaches_a_terminal_state(build):
    harness = build(completing(), max_concurrent=2)
    async with harness.client() as client:
        for _ in range(6):
            await submit(client)
        await harness.runner.drain()
        listing = (await client.get("/videos?limit=100")).json()

    assert listing["total"] == 6
    assert all(item["status"] in ("completed", "failed") for item in listing["items"])


# ---------------------------------------------------------------------------
# The real stub generator — touches ffmpeg, so it is not in the fast suite
# ---------------------------------------------------------------------------

@pytest.mark.slow
async def test_the_stub_generator_produces_a_real_playable_mp4(tmp_path):
    """CLAUDE.md §11 forbids reporting `completed` for a file with no audio
    stream. The round 4 placeholder is therefore a genuine video, and this is
    what proves it rather than assuming it.
    """
    from app.domain.job import Job
    from app.pipeline.stub import StubGenerator
    from app.providers import ffmpeg

    store = LocalArtifactStore(tmp_path)
    generate = StubGenerator(store, Settings())

    job = Job.create(query=PH_QUERY)
    job.concept = "ph_scale"

    async def report(stage):
        return None

    artifact = await generate(job, report)

    assert store.exists(job.job_id, VIDEO_NAME)
    assert store.exists(job.job_id, SCRIPT_NAME)
    assert store.exists(job.job_id, MANIFEST_NAME)

    probed = ffmpeg.probe(tmp_path / job.job_id / VIDEO_NAME)
    assert probed.has_video and probed.has_audio
    assert probed.duration_s > 0
    assert artifact.size_bytes > 0
