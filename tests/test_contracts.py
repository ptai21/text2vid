"""T3 — contracts with the outside world.

Every other test file runs against fakes, fixtures and hand-built bytes. That is
deliberate: it keeps the fast suite deterministic, offline and free. But it means
the fast suite can only prove that *our* rules are enforced. It cannot prove that
the assumptions those rules rest on are still true — that Gemini's constrained
generation fills in the fields we ask for, that its usage metadata still uses the
attribute names the cost model reads, that edge-tts returns something ffprobe can
measure.

Those assumptions have already been wrong once. `response_schema` declared
`params` as a bare object, constrained generation emitted only *declared*
properties, and every live call came back with empty params while all 325 fast
tests stayed green — because every fixture already had params populated. No
amount of mocking finds that. This file is where it would have been found.

Marked `slow`, excluded from the default run:

    uv run pytest -m slow

Skips — never fails — when a dependency is absent, because a missing API key is
a configuration state and not a broken contract. It does *not* skip when the
dependency is present and misbehaving. That is the entire point.
"""

from __future__ import annotations

import json
import shutil

import pytest

from app.concepts.registry import get_concept
from app.config import Settings
from app.domain.script import Script
from app.pipeline.cost import CostTracker, LLMUsage
from app.pipeline.retry import resolve_script
from app.pipeline.gates import (
    VISUAL_PARAMS,
    check_g1,
    check_g2,
    check_g3,
    check_g4,
    check_g5b,
)
from app.providers.ffmpeg import probe
from app.providers.llm import GeminiProvider
from app.providers.tts import EdgeTTSProvider

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def _settings() -> Settings:
    return Settings()


def requires_gemini() -> Settings:
    settings = _settings()
    if not settings.gemini_api_key:
        pytest.skip("GEMINI_API_KEY is not set")
    return settings


def requires_ffprobe() -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is not on PATH")


# ---------------------------------------------------------------------------
# Gemini
#
# One real call, shared. These assert different properties of the same
# response, so making three calls would cost three times as much and prove
# nothing extra.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_script():
    """One raw, ungated call. Asserted against for shape, not for quality."""
    settings = requires_gemini()
    concept = get_concept("ph_scale")
    raw = GeminiProvider(settings).generate_script(concept)
    return concept, raw


@pytest.fixture(scope="module")
def live_outcome():
    """The real model behind the real retry loop — the actual contract.

    Deliberately *not* a single call. Asserting that one live response clears
    every gate would be flaky by construction: the harness measured roughly one
    first-attempt rejection in eight, and the system is built so that this is
    unremarkable rather than fatal. What must hold is that the loop converges.
    """
    settings = requires_gemini()
    concept = get_concept("ph_scale")
    outcome = resolve_script(GeminiProvider(settings), concept, CostTracker())
    return concept, outcome


def test_the_live_model_behind_the_retry_loop_produces_a_usable_script(live_outcome):
    """The claim the whole pipeline rests on, against the real model.

    `test_gates.py` proves the gates reject bad output and `test_pipeline.py`
    proves the loop recovers from recorded failures. Neither can tell you
    whether the live model still converges — that is this test, and a
    `degraded` result is the signal that it has stopped.
    """
    concept, outcome = live_outcome

    assert isinstance(outcome.script, Script)
    assert outcome.degraded is False, (
        f"the live model failed {outcome.attempts} attempts and fell back: "
        f"{outcome.last_failure}"
    )
    assert 1 <= outcome.attempts <= 3

    script = outcome.script
    assert check_g2(script) is None
    assert check_g3(script, concept) is None
    assert check_g4(script, concept) is None


def test_a_raw_live_response_is_at_least_structurally_valid(live_script):
    """G1 separately from G2-G4, because they fail for different reasons.

    A G2 rejection is the model missing a budget and is expected occasionally.
    A G1 rejection means the response is not the declared schema at all, which
    would point at the SDK or the `response_schema` rather than at the model's
    writing — a different problem with a different fix.
    """
    _, raw = live_script

    # G1 returns the parsed Script on success and a GateFailure on failure -
    # the failure is data the retry prompt consumes, never an exception.
    script = check_g1(raw.text)
    assert isinstance(script, Script), script
    assert len(script.scenes) == 5


def test_every_visual_that_declares_params_arrives_with_them(live_script):
    """Regression for the bug that made this file necessary.

    Gemini's constrained generation emits **only declared properties**. When
    `response_schema` described `params` as a bare `{"type": "object"}`, every
    live response came back with `params == {}` and G3 rejected all three
    concepts — while the fast suite stayed green, because fixtures are written
    by hand and hand-written fixtures always have their params.

    A fake `LLMProvider` cannot fail this test. Only the real one can.

    Scoped to visuals that actually declare params: `title_card` declares none,
    because its text is injected by the renderer from `RenderContext` rather
    than written by the model. That is not an omission — it is the same
    guarantee that keeps the learner's raw query out of the prompt.
    """
    _, raw = live_script
    script = Script.model_validate_json(raw.text)

    expecting = [s for s in script.scenes if VISUAL_PARAMS[s.visual.type]]
    assert expecting, "no scene declared params, so this run proves nothing"

    empty = [s.scene_id for s in expecting if not s.visual.params]
    assert not empty, (
        f"scenes {empty} came back with no visual params — the response_schema "
        "is probably declaring an untyped object again"
    )


def test_usage_metadata_still_uses_the_field_names_the_cost_model_reads(live_script):
    """The seam where cost accounting would fail silently rather than loudly.

    `_count()` defaults a missing attribute to 0. That is correct behaviour for
    a non-thinking response, and it is also exactly what a renamed SDK field
    would look like: no error, no warning, every job billed at zero.
    """
    _, raw = live_script

    assert raw.prompt_tokens > 0, "prompt_token_count no longer reaches RawScript"
    assert raw.output_tokens > 0, "candidates_token_count no longer reaches RawScript"
    assert raw.thinking_tokens >= 0
    assert raw.model

    tracker = CostTracker()
    tracker.record_llm_call(
        LLMUsage(raw.prompt_tokens, raw.output_tokens, raw.thinking_tokens)
    )
    assert tracker.breakdown().llm_usd > 0, "a real call must cost more than nothing"


def test_the_response_is_json_and_nothing_else(live_script):
    """`response_mime_type` is what removes markdown-fence stripping from G1.

    If the model started wrapping output in ```json fences again, G1 would fail
    on every call and the fallback rate would go to 100% — a reliability
    collapse whose cause lives entirely in one config line.
    """
    _, raw = live_script
    json.loads(raw.text)
    assert not raw.text.lstrip().startswith("```")


# ---------------------------------------------------------------------------
# edge-tts and ffprobe
# ---------------------------------------------------------------------------

async def test_synthesis_returns_audio_that_ffprobe_can_measure(tmp_path):
    """Audio-first timing is only sound if the measurement is real.

    Two contracts in one: edge-tts returns a usable MP3, and `probe` reads a
    duration out of it. If either broke, every video's visual timing would be
    driven by a number that means nothing.
    """
    requires_ffprobe()
    settings = _settings()
    provider = EdgeTTSProvider(settings, cache_dir=tmp_path / "cache")

    text = (
        "The pH scale measures how acidic or basic a solution is, running from "
        "zero to fourteen with seven as neutral."
    )
    result = await provider.synthesize(text, tmp_path / "clip.mp3")

    assert result.path.exists()
    assert result.size_bytes > 1000
    # ~22 words. Any real narration pace puts this between 5 and 15 seconds;
    # the band is wide on purpose - this asserts "measured", not "fast".
    assert 5.0 < result.duration_s < 15.0, result.duration_s

    probed = probe(result.path)
    assert probed.has_audio
    assert abs(probed.duration_s - result.duration_s) < 0.1


async def test_a_live_script_narrates_inside_the_video_budget(live_outcome, tmp_path):
    """G5b against the real model, end to end.

    `test_pipeline.py` narrates a *fixture* through real TTS. This narrates
    what the model actually wrote today. The 45-90s window is a property of the
    combination - the word budget in G2 and the voice's pace - so only a live
    script measured through the live voice tests it.
    """
    requires_ffprobe()
    _, outcome = live_outcome
    script = outcome.script

    provider = EdgeTTSProvider(_settings(), cache_dir=tmp_path / "cache")
    durations = []
    for scene in script.scenes:
        result = await provider.synthesize(
            scene.narration, tmp_path / f"{scene.scene_id}.mp3"
        )
        durations.append(result.duration_s)

    total = sum(durations)
    assert check_g5b(total) is None, (
        f"a live script narrated to {total:.1f}s: {durations}"
    )


def test_ffprobe_reports_a_missing_file_as_an_error_not_a_zero(tmp_path):
    """The failure mode that would let a zero-byte artifact reach `completed`.

    G7 trusts `probe`. If probing a broken file returned a default-shaped
    result instead of raising, G7's duration and stream checks would pass on
    nothing at all.
    """
    requires_ffprobe()
    with pytest.raises(Exception):
        probe(tmp_path / "does_not_exist.mp4")
