"""Stub generator — round 4 only.

Proves the whole job lifecycle before any AI is involved. That separation is
deliberate (PLAN.md round 4): when generation later misbehaves, the job
machinery is already known-good, so failures are unambiguous.

It writes the full SPEC.md §12 bundle — `video.mp4`, `script.json`,
`manifest.json` — because the API contract exposes all three and a stub that
only produced one of them would leave two endpoints untested.

Round 6 replaces this with `app/pipeline/orchestrator.py`. The signature is
what round 6 has to satisfy, which is why the stage reporter is here from the
start rather than bolted on later.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.domain.job import ArtifactRef, Job, JobStage
from app.providers import ffmpeg
from app.storage.artifacts import (
    MANIFEST_NAME,
    SCRIPT_NAME,
    VIDEO_NAME,
    ArtifactStore,
)

PLACEHOLDER_SECONDS = 2.0
PLACEHOLDER_SCENES = 5


class StubGenerator:
    def __init__(self, store: ArtifactStore, settings: Settings):
        self._store = store
        self._settings = settings

    async def __call__(self, job: Job, report_stage) -> ArtifactRef:
        for stage in ("scripting", "narrating", "rendering", "muxing"):
            await report_stage(stage)

        # ffmpeg is a blocking subprocess; running it inline would stall the
        # event loop and make the concurrency semaphore meaningless.
        video_path, probed = await asyncio.to_thread(self._encode)

        await report_stage("publishing")

        try:
            self._store.put(job.job_id, VIDEO_NAME, video_path)
            self._put_json(job.job_id, SCRIPT_NAME, self._stub_script(job))
            self._put_json(job.job_id, MANIFEST_NAME, self._stub_manifest(job, probed))
        finally:
            video_path.unlink(missing_ok=True)

        return ArtifactRef(
            url=f"/videos/{job.job_id}/artifact",
            duration_s=probed.duration_s,
            size_bytes=probed.size_bytes,
            scenes=PLACEHOLDER_SCENES,
        )

    def _encode(self) -> tuple[Path, ffmpeg.ProbeResult]:
        handle = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        handle.close()
        path = Path(handle.name)

        ffmpeg.encode_placeholder(
            path,
            seconds=PLACEHOLDER_SECONDS,
            width=self._settings.video_width,
            height=self._settings.video_height,
            fps=self._settings.video_fps,
        )
        return path, ffmpeg.probe(path)

    def _put_json(self, job_id: str, name: str, payload: dict) -> None:
        """Written via a temp file so the generator only ever depends on the
        `ArtifactStore` Protocol (SPEC.md §12) and never on a concrete store."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle, indent=2)
            temp_path = Path(handle.name)
        try:
            self._store.put(job_id, name, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _stub_script(self, job: Job) -> dict:
        return {
            "concept": job.concept,
            "stub": True,
            "scenes": [],
        }

    def _stub_manifest(self, job: Job, probed: ffmpeg.ProbeResult) -> dict:
        """The §12 shape, honestly labelled.

        `"stub": true` and an empty `gates` list say plainly that no gate ran,
        rather than implying a clean pass. Round 8 fills this in for real.
        """
        return {
            "job_id": job.job_id,
            "query": job.query,
            "concept": job.concept,
            "stub": True,
            "degraded": job.degraded,
            "attempts": job.attempts,
            "gates": [],
            "scenes": [],
            "video": {
                "duration_s": probed.duration_s,
                "size_bytes": probed.size_bytes,
                "has_audio": probed.has_audio,
                "has_video": probed.has_video,
            },
            "cost": job.cost.model_dump(),
            "timings": job.timings,
            "model": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
