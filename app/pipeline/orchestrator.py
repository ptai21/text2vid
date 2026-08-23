"""Stage sequencing — SPEC.md §9.

Round 6 owns scripting and narrating. Rendering and muxing arrive in round 7
and slot in after `narrate`, which is why `NarratedScene` already carries the
per-scene duration the renderer will need.

**Audio-first timing.** The narration is synthesised first, its duration is
*measured*, and the visuals are then cut to fit it. The alternative — guessing
a duration from word count and hoping the speech lands close — is what makes
audio/video sync a calibration task. Measuring makes it arithmetic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.concepts.registry import ConceptContract
from app.domain.script import Scene, Script
from app.logging import get_logger
from app.pipeline.gates import (
    TRAILING_PAD_S,
    GateFailure,
    ScriptGateResult,
    check_g5a,
    check_g5b,
    run_script_gates,
)
from app.providers.llm import LLMProvider
from app.providers.tts import TTSError, TTSProvider

log = get_logger(__name__)

MAX_NARRATION_ATTEMPTS = 2
"""Attempts per scene *at the orchestrator level*.

The provider already retries network failures internally. This second loop
exists for the different case where synthesis succeeded but produced audio
that fails G5a — an empty or truncated file. Re-requesting it is the only
remedy, and one repeat is enough to separate a blip from a real problem.
"""


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
    def __init__(self, llm: LLMProvider, tts: TTSProvider):
        self._llm = llm
        self._tts = tts

    # -- scripting ---------------------------------------------------------

    def script(
        self, concept: ConceptContract, feedback: GateFailure | None = None
    ) -> tuple[ScriptGateResult, object]:
        """One attempt: generate, then run G1-G4.

        Deliberately a single attempt. The retry-with-feedback policy and the
        fallback are round 8's job, and keeping them out of here means the
        policy can be tested without a model in the loop.
        """
        raw = self._llm.generate_script(concept, feedback)
        return run_script_gates(raw.text, concept), raw

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
