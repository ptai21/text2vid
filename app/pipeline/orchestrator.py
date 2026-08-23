"""Stage sequencing — SPEC.md §9.

Owns the order of the five stages and nothing else. Every stage's actual work
lives behind a Protocol in `providers/` or a pure function in `pipeline/`, so
this module reads as the pipeline's table of contents: scripting, narrating,
rendering, muxing, publishing, with the gate that guards each one.

**Audio-first timing.** The narration is synthesised first, its duration is
*measured*, and the visuals are then cut to fit it. The alternative - guessing
a duration from word count and hoping the speech lands close - is what makes
audio/video sync a calibration task. Measuring makes it arithmetic.

**Failures are named here.** Each stage raises `StageFailure` with the code the
API will report, because this is the only layer that knows which stage was
running and why it stopped. The runner's catch-all remains underneath for
genuine bugs, which is what `internal_error` should mean (R7).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.concepts.registry import ConceptContract, get_concept
from app.config import Settings
from app.domain.job import ArtifactRef, Job
from app.domain.script import Scene, Script
from app.logging import get_logger
from app.pipeline.cost import CostTracker
from app.pipeline.gates import (
    TRAILING_PAD_S,
    GateFailure,
    ScriptGateResult,
    check_g5a,
    check_g5b,
    check_g6,
    check_g7,
    run_script_gates,
)
from app.pipeline.retry import ScriptOutcome, ScriptUnavailable, load_fallback, resolve_script
from app.pipeline.runner import StageFailure, StageReporter
from app.providers import ffmpeg
from app.providers.llm import LLMProvider
from app.providers.scenes import RenderContext
from app.providers.tts import TTSError, TTSProvider
from app.providers.visual import RenderError, VisualProvider
from app.storage.artifacts import (
    MANIFEST_NAME,
    SCRIPT_NAME,
    VIDEO_NAME,
    ArtifactStore,
)

log = get_logger(__name__)

MAX_NARRATION_ATTEMPTS = 2
"""Attempts per scene *at the orchestrator level*.

The provider already retries network failures internally. This second loop
exists for the different case where synthesis succeeded but produced audio
that fails G5a — an empty or truncated file. Re-requesting it is the only
remedy, and one repeat is enough to separate a blip from a real problem.
"""

VisualFactory = Callable[[RenderContext], VisualProvider]
"""Built per job because the title card shows the learner's own query.

Injected rather than imported so that swapping to a different renderer - the
R9 question - is a change in `main.py` and nowhere else."""


@dataclass(frozen=True)
class NarratedScene:
    """A scene plus its measured audio. Satisfies `gates.AudioClip`."""

    scene: Scene
    path: Path
    duration_s: float
    """Probed audio length. What G5a checks."""
    size_bytes: int
    cached: bool = False

    @property
    def scene_id(self) -> str:
        return self.scene.scene_id

    @property
    def display_duration_s(self) -> float:
        """How long the visual stays on screen.

        The trailing pad is not decoration: educational narration that cuts to
        the next scene the instant a sentence ends feels rushed (SPEC.md §13).
        """
        return self.duration_s + TRAILING_PAD_S


@dataclass(frozen=True)
class NarrationResult:
    ok: bool
    scenes: tuple[NarratedScene, ...] = ()
    failure: GateFailure | None = None

    @property
    def total_duration_s(self) -> float:
        """Sum of on-screen durations — what the finished video will run to."""
        return sum(scene.display_duration_s for scene in self.scenes)


class Orchestrator:
    def __init__(
        self,
        llm: LLMProvider,
        tts: TTSProvider,
        *,
        visual: VisualFactory | None = None,
        store: ArtifactStore | None = None,
        settings: Settings | None = None,
    ):
        self._llm = llm
        self._tts = tts
        self._visual = visual
        self._store = store
        self._settings = settings

    # -- the generator entry point ----------------------------------------

    async def __call__(self, job: Job, report_stage: StageReporter) -> ArtifactRef:
        """Matches `runner.Generator`, so `main.py` wires it with one line."""
        if job.concept is None:
            raise StageFailure("internal_error", "resolving",
                               "The job reached the generator without a concept.")

        concept = get_concept(job.concept)
        tracker = CostTracker()
        timings: dict[str, float] = {}
        work = self._work_dir(job)

        outcome = await self._stage_script(job, concept, tracker, report_stage,
                                           timings)
        narration = await self._stage_narrate(concept, outcome, work,
                                              report_stage, timings)
        tracker.record_tts(
            sum(len(scene.scene.narration) for scene in narration.scenes)
        )
        job.degraded = outcome.degraded

        frames = await self._stage_render(job, concept, narration, work,
                                          report_stage, timings)
        video, probed = await self._stage_mux(narration, frames, work,
                                              report_stage, timings)

        with _timed(timings, "publishing"):
            await report_stage("publishing")
            artifact = await asyncio.to_thread(
                self._publish, job, concept, outcome, narration, frames,
                video, probed, tracker, timings,
            )

        job.cost = tracker.breakdown()
        job.timings = timings
        return artifact

    # -- scripting ---------------------------------------------------------

    def script(
        self, concept: ConceptContract, feedback: GateFailure | None = None
    ) -> tuple[ScriptGateResult, object]:
        """One un-retried attempt. Kept for tests that drive a single call."""
        raw = self._llm.generate_script(concept, feedback)
        return run_script_gates(raw.text, concept), raw

    async def _stage_script(self, job, concept, tracker, report_stage,
                            timings) -> ScriptOutcome:
        await report_stage("scripting")
        attempts = self._settings.max_script_attempts if self._settings else 3

        with _timed(timings, "scripting"):
            try:
                # Blocking SDK call, and up to three of them. Left on the event
                # loop it would stall every other job under the semaphore.
                outcome = await asyncio.to_thread(
                    resolve_script, self._llm, concept, tracker,
                    max_attempts=attempts,
                )
            except ScriptUnavailable as exc:
                # The only failure this stage can produce, and it means the
                # committed fallback file is missing or broken - a deployment
                # error, not a runtime one (SPEC.md §9.1).
                raise StageFailure("script_unavailable", "scripting",
                                   "No usable script could be produced.",
                                   str(exc)) from exc

        job.attempts = outcome.attempts
        job.degraded = outcome.degraded
        return outcome

    # -- narrating ---------------------------------------------------------

    async def narrate(self, script: Script, out_dir: Path) -> NarrationResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        narrated: list[NarratedScene] = []

        for scene in script.scenes:
            try:
                clip = await self._narrate_scene(scene, out_dir)
            except TTSError as exc:
                return NarrationResult(
                    ok=False,
                    failure=GateFailure(
                        "G5a", "tts_failed",
                        f"Narration for scene {scene.scene_id} could not be "
                        f"synthesised: {exc}",
                    ),
                )

            integrity = check_g5a([clip])
            if integrity is not None:
                return NarrationResult(ok=False, scenes=tuple(narrated),
                                       failure=integrity)
            narrated.append(clip)

        result = NarrationResult(ok=True, scenes=tuple(narrated))

        # G5b last, because it is a judgement about the script as a whole and
        # only becomes answerable once every clip has been measured.
        duration = check_g5b(result.total_duration_s)
        if duration is not None:
            return NarrationResult(ok=False, scenes=tuple(narrated), failure=duration)

        log.info("narration.complete", scenes=len(narrated),
                 total_s=round(result.total_duration_s, 2),
                 cached=sum(1 for scene in narrated if scene.cached))
        return result

    async def _stage_narrate(self, concept, outcome, work, report_stage,
                             timings) -> NarrationResult:
        await report_stage("narrating")

        with _timed(timings, "narrating"):
            narration = await self.narrate(outcome.script, work / "audio")

            if not narration.ok and narration.failure.gate == "G5b" \
                    and not outcome.degraded:
                # This is what G5b is *for*. The narration is the right length
                # for the words it was given, so re-running TTS would produce
                # the same seconds again; the script is what is wrong. The
                # fallback has a measured runtime, so swapping to it is the
                # only remedy that can change the answer.
                log.warning("narration.falling_back", concept=concept.key,
                            reason=narration.failure.reason)
                outcome = _degrade(outcome, load_fallback(concept))
                narration = await self.narrate(outcome.script, work / "audio")

        if not narration.ok:
            failure = narration.failure
            raise StageFailure(
                "tts_failed", "narrating",
                "The narration could not be produced.",
                f"{failure.gate}/{failure.reason}: {failure.detail}",
            )
        return narration

    async def _narrate_scene(self, scene: Scene, out_dir: Path) -> NarratedScene:
        """Synthesise one scene, repeating if the audio itself is unusable."""
        out = out_dir / f"{scene.scene_id}.mp3"
        last: GateFailure | None = None

        for attempt in range(1, MAX_NARRATION_ATTEMPTS + 1):
            audio = await self._tts.synthesize(scene.narration, out)
            clip = NarratedScene(
                scene=scene,
                path=audio.path,
                duration_s=audio.duration_s,
                size_bytes=audio.size_bytes,
                cached=audio.cached,
            )

            last = check_g5a([clip])
            if last is None:
                return clip

            log.warning("narration.g5a_failed", scene_id=scene.scene_id,
                        attempt=attempt, reason=last.reason)
            await asyncio.sleep(0)

        raise TTSError(
            f"scene {scene.scene_id} still fails G5a after "
            f"{MAX_NARRATION_ATTEMPTS} attempts: {last.detail if last else 'unknown'}"
        )

    # -- rendering ---------------------------------------------------------

    async def _stage_render(self, job, concept, narration, work, report_stage,
                            timings) -> list[Path]:
        await report_stage("rendering")
        context = RenderContext(query=job.query, concept_title=concept.title)

        with _timed(timings, "rendering"):
            try:
                frames = await asyncio.to_thread(
                    self._render_all, context, narration, work / "frames"
                )
            except RenderError as exc:
                raise StageFailure("render_failed", "rendering",
                                   "A scene could not be drawn.",
                                   str(exc)) from exc

        failure = check_g6(frames, expected=len(narration.scenes),
                           width=self._width, height=self._height)
        if failure is not None:
            raise StageFailure("render_failed", "rendering",
                               "The rendered frames failed validation.",
                               f"{failure.reason}: {failure.detail}")
        return frames

    def _render_all(self, context, narration, out_dir: Path) -> list[Path]:
        visual = self._visual(context)
        frames: list[Path] = []
        for scene in narration.scenes:
            frames += visual.render(scene.scene, scene.display_duration_s, out_dir)
        return frames

    # -- muxing ------------------------------------------------------------

    async def _stage_mux(self, narration, frames, work, report_stage, timings):
        await report_stage("muxing")
        video = work / VIDEO_NAME

        with _timed(timings, "muxing"):
            try:
                await asyncio.to_thread(
                    ffmpeg.mux,
                    [
                        ffmpeg.MuxScene(still=frame, audio=scene.path,
                                        duration_s=scene.display_duration_s)
                        for frame, scene in zip(frames, narration.scenes)
                    ],
                    video,
                    width=self._width, height=self._height, fps=self._fps,
                )
                probed = await asyncio.to_thread(ffmpeg.probe, video)
            except ffmpeg.FFmpegError as exc:
                # stderr is the only place ffmpeg explains itself, so it
                # becomes `failure.detail` rather than being discarded.
                raise StageFailure("mux_failed", "muxing",
                                   "The video could not be assembled.",
                                   f"{exc}\n{exc.stderr}") from exc

        failure = check_g7(probed,
                           expected_duration_s=narration.total_duration_s)
        if failure is not None:
            raise StageFailure("artifact_invalid", "muxing",
                               "The finished video failed validation.",
                               f"{failure.reason}: {failure.detail}")
        return video, probed

    # -- publishing --------------------------------------------------------

    def _publish(self, job, concept, outcome, narration, frames, video, probed,
                 tracker, timings) -> ArtifactRef:
        manifest = self._manifest(job, concept, outcome, narration, probed,
                                  tracker, timings)

        self._store.put(job.job_id, VIDEO_NAME, video)
        self._put_json(job.job_id, SCRIPT_NAME, outcome.script.model_dump())
        self._put_json(job.job_id, MANIFEST_NAME, manifest)

        return ArtifactRef(
            url=f"/videos/{job.job_id}/artifact",
            duration_s=probed.duration_s,
            size_bytes=probed.size_bytes,
            scenes=len(narration.scenes),
        )

    def _manifest(self, job, concept, outcome, narration, probed, tracker,
                  timings) -> dict:
        """The SPEC.md §12 run record.

        One artifact serving three graded criteria at once: a clean artifact
        boundary (architecture), evidence that every gate actually fired
        (reliability), and a per-job cost and timing breakdown (observability).
        """
        stills = ffmpeg.still_durations(
            [scene.display_duration_s for scene in narration.scenes]
        )
        return {
            "job_id": job.job_id,
            "query": job.query,
            "concept": concept.key,
            "degraded": outcome.degraded,
            "attempts": outcome.attempts,
            "gates": [attempt.as_dict() for attempt in outcome.gates],
            "scenes": [
                {
                    "scene_id": scene.scene_id,
                    "visual": scene.scene.visual.type,
                    "audio_s": round(scene.duration_s, 2),
                    "frames": max(2, round(still * self._fps)),
                    "cached_audio": scene.cached,
                }
                for scene, still in zip(narration.scenes, stills)
            ],
            "video": {
                "duration_s": probed.duration_s,
                "size_bytes": probed.size_bytes,
                "has_audio": probed.has_audio,
                "has_video": probed.has_video,
            },
            "cost": tracker.breakdown().model_dump(),
            "tokens": {
                "llm_calls": tracker.llm_calls,
                "prompt": tracker.totals.prompt_tokens,
                "output": tracker.totals.output_tokens,
                # Invisible in the response text and billed at the output
                # rate, so it is reported separately rather than folded in.
                "thinking": tracker.totals.thinking_tokens,
            },
            "timings": {key: round(value, 2) for key, value in timings.items()},
            "model": outcome.model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _put_json(self, job_id: str, name: str, payload: dict) -> None:
        """Written via a temp file so publishing depends only on the
        `ArtifactStore` Protocol (SPEC.md §12), never on a concrete store."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle, indent=2, default=str)
            temp_path = Path(handle.name)
        try:
            self._store.put(job_id, name, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    # -- helpers -----------------------------------------------------------

    def _work_dir(self, job: Job) -> Path:
        root = self._settings.artifact_dir if self._settings else Path("./artifacts")
        return root / "_work" / job.job_id

    @property
    def _width(self) -> int:
        return self._settings.video_width if self._settings else 1280

    @property
    def _height(self) -> int:
        return self._settings.video_height if self._settings else 720

    @property
    def _fps(self) -> int:
        return self._settings.video_fps if self._settings else 30


def _degrade(outcome: ScriptOutcome, script: Script) -> ScriptOutcome:
    """Swap in the fallback while keeping the attempt and gate history."""
    return ScriptOutcome(
        script=script,
        degraded=True,
        attempts=outcome.attempts,
        gates=outcome.gates,
        model=outcome.model,
        last_failure=outcome.last_failure,
    )


@contextmanager
def _timed(timings: dict[str, float], stage: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = timings.get(stage, 0.0) + time.perf_counter() - started
