"""Narration, audio-first timing and gate G5 — SPEC.md §9.2 and §10.

T2: fake providers, no network. The real edge-tts call lives in one
`@pytest.mark.slow` test at the bottom.

The claim these tests defend is that **duration is measured, never
estimated**. Everything downstream — scene timing, G5b, the final video
length — is arithmetic on a probed number, so a word-count guess anywhere in
the chain would quietly desynchronise every video.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from app.concepts.registry import get_concept
from app.config import Settings
from app.domain.job import Job
from app.domain.script import Scene, Script, VisualSpec
from app.pipeline.cost import CostTracker
from app.pipeline.gates import TRAILING_PAD_S, check_g5a, check_g5b
from app.pipeline.orchestrator import NarratedScene, Orchestrator
from app.pipeline.retry import (
    ScriptOutcome,
    ScriptUnavailable,
    load_fallback,
    resolve_script,
)
from app.pipeline.runner import JobRunner, StageFailure
from app.providers.ffmpeg import ProbeResult, probe as ffmpeg_probe
from app.providers.llm import RawScript
from app.providers.tts import AudioResult, EdgeTTSProvider, TTSError
from app.storage.repository import InMemoryJobRepository

WORDS = (
    "Every water based solution contains hydrogen ions and pH is simply the "
    "number we use to record how crowded a solution really is today"
)


def a_scene(scene_id="s1", narration=WORDS) -> Scene:
    return Scene(
        scene_id=scene_id,
        heading="What pH measures",
        narration=narration,
        visual=VisualSpec(type="title_card", params={}),
    )


def a_script(count=5) -> Script:
    return Script(
        concept="ph_scale",
        scenes=[a_scene(f"s{index}") for index in range(1, count + 1)],
    )


class FakeTTS:
    """A `TTSProvider` implementation, not a mock of one.

    `durations` drives what each successive call reports, which is how a test
    can make the measured length disagree with the word count on purpose.
    """

    def __init__(self, durations=None, *, size_bytes=4096, write=True):
        self.durations = list(durations) if durations is not None else None
        self.size_bytes = size_bytes
        self.write = write
        self.calls: list[str] = []

    async def synthesize(self, text: str, out: Path) -> AudioResult:
        self.calls.append(text)
        if self.write:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00" * max(self.size_bytes, 0))

        if self.durations is None:
            duration = 12.0
        else:
            index = min(len(self.calls) - 1, len(self.durations) - 1)
            duration = self.durations[index]

        return AudioResult(
            path=out, duration_s=duration, size_bytes=self.size_bytes, cached=False
        )


def a_clip(scene_id="s1", *, path: Path, duration_s=12.0, size_bytes=4096):
    return NarratedScene(
        scene=a_scene(scene_id), path=path, duration_s=duration_s,
        size_bytes=size_bytes,
    )


@pytest.fixture
def audio_file(tmp_path) -> Path:
    path = tmp_path / "s1.mp3"
    path.write_bytes(b"\x00" * 4096)
    return path


# ---------------------------------------------------------------------------
# Audio-first timing
# ---------------------------------------------------------------------------

async def test_duration_comes_from_the_measurement_not_the_word_count(tmp_path):
    """The load-bearing test of this file.

    Every narration here is the same 25 words, but the measurement says 3.0s
    for one scene and 20.0s for another. If anything estimated from words, the
    two would come back equal.
    """
    measured = [3.0, 20.0, 7.5, 11.25, 4.0]
    orchestrator = Orchestrator(llm=None, tts=FakeTTS(durations=measured))

    result = await orchestrator.narrate(a_script(), tmp_path)

    assert result.ok
    assert [scene.duration_s for scene in result.scenes] == measured


async def test_each_scene_gets_a_trailing_pad(tmp_path):
    """Narration that cuts the instant a sentence ends feels rushed."""
    orchestrator = Orchestrator(llm=None, tts=FakeTTS(durations=[10.0]))
    result = await orchestrator.narrate(a_script(1), tmp_path)

    scene = result.scenes[0]
    assert scene.duration_s == 10.0
    assert scene.display_duration_s == pytest.approx(10.0 + TRAILING_PAD_S)


async def test_total_duration_is_the_sum_of_on_screen_time(tmp_path):
    orchestrator = Orchestrator(llm=None, tts=FakeTTS(durations=[12.0]))
    result = await orchestrator.narrate(a_script(), tmp_path)

    assert result.total_duration_s == pytest.approx(5 * (12.0 + TRAILING_PAD_S))


async def test_one_audio_file_is_written_per_scene(tmp_path):
    orchestrator = Orchestrator(llm=None, tts=FakeTTS(durations=[12.0]))
    result = await orchestrator.narrate(a_script(), tmp_path)

    assert len(result.scenes) == 5
    assert {scene.path.name for scene in result.scenes} == {
        f"s{index}.mp3" for index in range(1, 6)
    }


# ---------------------------------------------------------------------------
# G5a — integrity. Remedy: retry TTS.
# ---------------------------------------------------------------------------

def test_g5a_rejects_a_missing_file(tmp_path):
    failure = check_g5a([a_clip(path=tmp_path / "absent.mp3")])
    assert failure.gate == "G5a" and failure.reason == "audio_missing"


def test_g5a_rejects_a_zero_byte_file(audio_file):
    failure = check_g5a([a_clip(path=audio_file, size_bytes=0)])
    assert failure.reason == "audio_empty"


@pytest.mark.parametrize("duration", [0.5, 1.9, 25.1, 90.0])
def test_g5a_rejects_a_clip_outside_the_per_scene_bounds(audio_file, duration):
    failure = check_g5a([a_clip(path=audio_file, duration_s=duration)])
    assert failure.reason == "clip_duration"


@pytest.mark.parametrize("duration", [2.0, 12.0, 25.0])
def test_g5a_accepts_a_clip_inside_the_bounds(audio_file, duration):
    assert check_g5a([a_clip(path=audio_file, duration_s=duration)]) is None


async def test_a_scene_whose_audio_fails_g5a_is_synthesised_again(tmp_path):
    """s1's first attempt returns a truncated 0.5s clip; the retry is fine.

    A full five-scene script, so the run also has to clear G5b — otherwise
    this would pass for the wrong reason on a script too short to be a video.
    """
    tts = FakeTTS(durations=[0.5] + [12.0] * 5)
    orchestrator = Orchestrator(llm=None, tts=tts)

    result = await orchestrator.narrate(a_script(), tmp_path)

    assert result.ok, result.failure and result.failure.detail
    assert len(tts.calls) == 6, "a bad clip must be re-requested, not accepted"
    assert result.scenes[0].duration_s == 12.0


async def test_narration_fails_when_retries_cannot_produce_usable_audio(tmp_path):
    """R5: a video without narration is not a valid outcome, so this fails the
    job rather than degrading."""
    tts = FakeTTS(durations=[0.4])
    result = await Orchestrator(llm=None, tts=tts).narrate(a_script(1), tmp_path)

    assert not result.ok
    assert result.failure.gate == "G5a"
    assert result.failure.reason == "tts_failed"


# ---------------------------------------------------------------------------
# G5b — total duration. Remedy: the fallback script, NOT a TTS retry.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("total", [44.9, 90.1, 120.0])
def test_g5b_rejects_a_total_outside_the_video_bounds(total):
    failure = check_g5b(total)
    assert failure.gate == "G5b" and failure.reason == "total_duration"


@pytest.mark.parametrize("total", [45.0, 68.0, 90.0])
def test_g5b_accepts_a_total_inside_the_video_bounds(total):
    assert check_g5b(total) is None


async def test_a_script_that_narrates_too_long_fails_g5b_not_g5a(tmp_path):
    """Five valid 22s clips: every clip passes G5a, the sum does not pass G5b.

    The distinction is the entire reason the gate is split. Reporting this as
    G5a would send the pipeline back to TTS.
    """
    tts = FakeTTS(durations=[22.0])
    result = await Orchestrator(llm=None, tts=tts).narrate(a_script(), tmp_path)

    assert not result.ok
    assert result.failure.gate == "G5b"
    assert result.total_duration_s > 90.0


async def test_g5b_does_not_re_run_tts(tmp_path):
    """Re-synthesising the same words produces the same seconds, forever.

    Exactly five calls for five scenes — a sixth would mean the pipeline was
    retrying something a retry cannot fix.
    """
    tts = FakeTTS(durations=[22.0])
    await Orchestrator(llm=None, tts=tts).narrate(a_script(), tmp_path)

    assert len(tts.calls) == 5


async def test_a_g5b_failure_still_reports_what_was_measured(tmp_path):
    """Round 8 needs the measurement to decide, and the manifest records it."""
    tts = FakeTTS(durations=[22.0])
    result = await Orchestrator(llm=None, tts=tts).narrate(a_script(), tmp_path)

    assert len(result.scenes) == 5
    assert "s" in result.failure.detail or result.failure.detail


def test_the_two_halves_of_g5_are_distinguishable(audio_file):
    """A caller that cannot tell them apart cannot choose the right remedy."""
    integrity = check_g5a([a_clip(path=audio_file, duration_s=0.5)])
    duration = check_g5b(200.0)

    assert integrity.gate != duration.gate
    assert integrity.reason != duration.reason


# ---------------------------------------------------------------------------
# Caching — SPEC.md §9.2, the highest-leverage cost lever
# ---------------------------------------------------------------------------

def a_provider(tmp_path, *, calls: list, voice="en-US-AriaNeural", rate="+0%"):
    async def speak(text, voice_id, rate_value, out):
        calls.append((text, voice_id, rate_value))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 4096)

    def probe(path):
        return ProbeResult(duration_s=12.0, size_bytes=path.stat().st_size,
                           has_video=False, has_audio=True)

    settings = Settings(artifact_dir=tmp_path, tts_voice=voice, tts_rate=rate)
    return EdgeTTSProvider(
        settings, cache_dir=tmp_path / "cache", backoff=(0.0, 0.0, 0.0),
        speak=speak, probe=probe,
    )


async def test_identical_narration_is_synthesised_once(tmp_path):
    calls: list = []
    provider = a_provider(tmp_path, calls=calls)

    first = await provider.synthesize(WORDS, tmp_path / "a.mp3")
    second = await provider.synthesize(WORDS, tmp_path / "b.mp3")

    assert len(calls) == 1, "the second request must come from cache"
    assert first.cached is False
    assert second.cached is True
    assert (tmp_path / "b.mp3").read_bytes() == (tmp_path / "a.mp3").read_bytes()


async def test_different_narration_is_synthesised_separately(tmp_path):
    calls: list = []
    provider = a_provider(tmp_path, calls=calls)

    await provider.synthesize(WORDS, tmp_path / "a.mp3")
    await provider.synthesize(WORDS + " and more", tmp_path / "b.mp3")

    assert len(calls) == 2


def test_the_cache_key_covers_the_voice_and_the_rate(tmp_path):
    """Same words in a different voice are a different recording."""
    base = a_provider(tmp_path, calls=[])
    other_voice = a_provider(tmp_path, calls=[], voice="en-GB-SoniaNeural")
    other_rate = a_provider(tmp_path, calls=[], rate="+10%")

    assert base.cache_key(WORDS) != other_voice.cache_key(WORDS)
    assert base.cache_key(WORDS) != other_rate.cache_key(WORDS)


# ---------------------------------------------------------------------------
# Network retry policy — SPEC.md §9.2
# ---------------------------------------------------------------------------

async def test_a_transient_network_failure_is_retried(tmp_path):
    attempts = {"count": 0}

    async def flaky(text, voice, rate, out):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("edge-tts dropped the socket")
        out.write_bytes(b"\x00" * 4096)

    def probe(path):
        return ProbeResult(12.0, path.stat().st_size, False, True)

    provider = EdgeTTSProvider(
        Settings(artifact_dir=tmp_path), cache_dir=tmp_path / "cache",
        backoff=(0.0, 0.0, 0.0), speak=flaky, probe=probe,
    )

    result = await provider.synthesize(WORDS, tmp_path / "a.mp3")
    assert attempts["count"] == 3
    assert result.duration_s == 12.0


async def test_exhausted_retries_raise_tts_failed(tmp_path):
    async def always_fails(text, voice, rate, out):
        raise ConnectionError("edge-tts is unreachable")

    provider = EdgeTTSProvider(
        Settings(artifact_dir=tmp_path), cache_dir=tmp_path / "cache",
        backoff=(0.0, 0.0, 0.0), speak=always_fails,
    )

    with pytest.raises(TTSError):
        await provider.synthesize(WORDS, tmp_path / "a.mp3")


async def test_a_hanging_call_is_not_waited_on_forever(tmp_path):
    """edge-tts is an unofficial endpoint with no default timeout."""
    import asyncio

    async def hangs(text, voice, rate, out):
        await asyncio.sleep(3600)

    provider = EdgeTTSProvider(
        Settings(artifact_dir=tmp_path), cache_dir=tmp_path / "cache",
        backoff=(0.0,), timeout_s=0.05, speak=hangs,
    )

    with pytest.raises(TTSError):
        await provider.synthesize(WORDS, tmp_path / "a.mp3")


async def test_an_empty_file_counts_as_a_failed_attempt(tmp_path):
    """A silent success is the worst kind: it corrupts everything after it."""
    calls = {"count": 0}

    async def writes_nothing(text, voice, rate, out):
        calls["count"] += 1
        out.write_bytes(b"")

    provider = EdgeTTSProvider(
        Settings(artifact_dir=tmp_path), cache_dir=tmp_path / "cache",
        backoff=(0.0, 0.0, 0.0), speak=writes_nothing,
    )

    with pytest.raises(TTSError):
        await provider.synthesize(WORDS, tmp_path / "a.mp3")
    assert calls["count"] == 3


# ---------------------------------------------------------------------------
# One real run — real edge-tts, real ffprobe
# ---------------------------------------------------------------------------

@pytest.mark.slow
async def test_a_real_gated_script_narrates_inside_the_video_budget(tmp_path):
    """PLAN.md round 6 Verify: "then one real run".

    This is the test that validates the *whole timing argument*. G2 enforces a
    125-190 word budget on the claim that it keeps the finished video inside
    45-90 seconds. Nothing verified that claim against real speech until now —
    it was arithmetic on an assumed words-per-minute rate.

    Real script, real edge-tts, real ffprobe. If the budget were wrong, G5b
    would fire here and every production run would land in the fallback.
    """
    import json

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "llm" / "valid_ph.json")
        .read_text(encoding="utf-8")
    )
    script = Script.model_validate(fixture)

    provider = EdgeTTSProvider(
        Settings(artifact_dir=tmp_path), cache_dir=tmp_path / "cache"
    )
    result = await Orchestrator(llm=None, tts=provider).narrate(
        script, tmp_path / "audio"
    )

    assert result.ok, result.failure and result.failure.detail
    assert len(result.scenes) == 5

    for clip in result.scenes:
        assert clip.path.is_file() and clip.size_bytes > 0
        assert 2.0 <= clip.duration_s <= 25.0

    words = script.total_words
    implied_wpm = words / (sum(c.duration_s for c in result.scenes) / 60)
    assert 90 <= implied_wpm <= 260, (
        f"{words} words in {result.total_duration_s:.1f}s is {implied_wpm:.0f} wpm, "
        "which is not human narration pace"
    )


@pytest.mark.slow
async def test_the_real_cache_survives_a_second_pass(tmp_path):
    """What protects the free-tier quota across a fifteen-run harness."""
    provider = EdgeTTSProvider(
        Settings(artifact_dir=tmp_path), cache_dir=tmp_path / "cache"
    )

    first = await provider.synthesize(WORDS, tmp_path / "a.mp3")
    second = await provider.synthesize(WORDS, tmp_path / "b.mp3")

    assert first.cached is False
    assert second.cached is True
    assert second.duration_s == pytest.approx(first.duration_s)


# ===========================================================================
# Round 8 — retry with feedback, the fallback, cost and the manifest
# ===========================================================================

FIXTURES = Path(__file__).parent / "fixtures" / "llm"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeLLM:
    """An `LLMProvider` implementation, not a mock of one.

    It records the feedback it was handed on each call, which is how the tests
    below assert that a retry is *informed* rather than simply repeated - the
    distinction SPEC.md §8 says separates engineering from a second lottery
    ticket. Pass an exception in `responses` to simulate a provider outage.
    """

    def __init__(self, responses, *, model="fake-model", thinking=500):
        self.responses = list(responses)
        self.model = model
        self.thinking = thinking
        self.feedback: list[object] = []
        self.calls = 0

    def generate_script(self, concept, feedback=None):
        self.feedback.append(feedback)
        self.calls += 1
        item = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return RawScript(text=item, prompt_tokens=1500, output_tokens=1000,
                         thinking_tokens=self.thinking, model=self.model)


def resolve(responses, concept_key="ph_scale", **kwargs):
    llm = FakeLLM(responses)
    tracker = CostTracker()
    outcome = resolve_script(llm, get_concept(concept_key), tracker, **kwargs)
    return outcome, llm, tracker


# ---------------------------------------------------------------------------
# The retry policy — SPEC.md §9.1
# ---------------------------------------------------------------------------

def test_a_script_that_passes_the_gates_is_used_immediately():
    outcome, llm, _ = resolve([fixture("valid_ph.json")])

    assert llm.calls == 1
    assert outcome.attempts == 1
    assert outcome.degraded is False
    assert outcome.script.total_words >= 125


def test_the_first_attempt_carries_no_feedback():
    """There is nothing to say yet, and inventing something would bias it."""
    _, llm, _ = resolve([fixture("valid_ph.json")])
    assert llm.feedback == [None]


def test_a_failed_gate_is_retried_with_feedback_naming_it():
    """The justification R8 demands for keeping the retry at all.

    Attempt 1 drops the logarithm beat and fails G4. Attempt 2 must be told
    which gate failed and what was missing, or the retry is just another roll
    of the same dice.
    """
    outcome, llm, _ = resolve(
        [fixture("g4_ph_no_logarithm.json"), fixture("valid_ph.json")]
    )

    assert llm.calls == 2
    assert outcome.degraded is False

    feedback = llm.feedback[1]
    assert feedback is not None
    assert feedback.gate == "G4"
    assert "logarith" in feedback.detail.lower()


def test_three_failures_fall_back_rather_than_failing_the_job():
    """SPEC.md §9.1: this stage cannot fail. Proven, not asserted."""
    outcome, llm, _ = resolve([fixture("g4_ph_no_logarithm.json")])

    assert llm.calls == 3
    assert outcome.attempts == 3
    assert outcome.degraded is True
    assert outcome.script.concept == "ph_scale"


def test_the_fallback_is_never_silent():
    """CLAUDE.md §11 draws the line here.

    A pre-committed fallback is a legitimate degradation path; the same
    fallback served without the flag would be canned output pretending to be
    generation.
    """
    outcome, _, _ = resolve([fixture("g1_not_json.txt")])
    assert outcome.degraded is True
    assert outcome.last_failure is not None
    assert outcome.last_failure.gate == "G1"


def test_a_provider_outage_still_produces_a_script():
    """The model being unreachable and the model being wrong end the same way.

    From the pipeline's point of view both mean "no usable script", and a
    learner should not be able to tell the difference from the outcome.
    """
    outcome, llm, tracker = resolve([RuntimeError("503 backend unavailable")])

    assert llm.calls == 3
    assert outcome.degraded is True
    assert tracker.llm_calls == 0, "a call that raised produced no billable usage"


def test_a_provider_that_recovers_is_not_penalised():
    outcome, llm, _ = resolve(
        [RuntimeError("transient"), fixture("valid_ph.json")]
    )
    assert llm.calls == 2
    assert outcome.degraded is False


def test_the_attempt_ceiling_is_configurable():
    outcome, llm, _ = resolve([fixture("g1_not_json.txt")], max_attempts=2)
    assert llm.calls == 2
    assert outcome.attempts == 2
    assert outcome.degraded is True


# ---------------------------------------------------------------------------
# The gate log that becomes manifest.json — SPEC.md §12
# ---------------------------------------------------------------------------

def test_the_gate_log_shows_which_gates_ran_on_each_attempt():
    """Gates stop at the first failure, so the log must not imply otherwise.

    A G4 failure means G1-G3 passed. Recording only "G4 failed" would lose
    that, and recording all four as run on a G1 failure would be a lie.
    """
    outcome, _, _ = resolve(
        [fixture("g4_ph_no_logarithm.json"), fixture("valid_ph.json")]
    )

    first = [row for row in outcome.gates if row.attempt == 1]
    assert [(row.gate, row.passed) for row in first] == [
        ("G1", True), ("G2", True), ("G3", True), ("G4", False)
    ]

    second = [row for row in outcome.gates if row.attempt == 2]
    assert all(row.passed for row in second)
    assert len(second) == 4


def test_a_g1_failure_stops_the_log_at_g1():
    outcome, _, _ = resolve([fixture("g1_not_json.txt"), fixture("valid_ph.json")])

    first = [row for row in outcome.gates if row.attempt == 1]
    assert [row.gate for row in first] == ["G1"]
    assert first[0].passed is False


def test_the_gate_log_serialises_with_a_reason_only_when_it_failed():
    outcome, _, _ = resolve(
        [fixture("g2_four_scenes.json"), fixture("valid_ph.json")]
    )
    rows = [row.as_dict() for row in outcome.gates]

    failed = [row for row in rows if not row["passed"]]
    assert len(failed) == 1
    assert "reason" in failed[0]
    assert all("reason" not in row for row in rows if row["passed"])


# ---------------------------------------------------------------------------
# Cost — R10 and SPEC.md §14
# ---------------------------------------------------------------------------

def test_every_attempt_is_billed_including_the_ones_that_failed():
    """Under-counting retries would make the degraded path look free."""
    _, _, tracker = resolve([fixture("g4_ph_no_logarithm.json")])

    assert tracker.llm_calls == 3
    assert tracker.totals.thinking_tokens == 1500
    assert tracker.breakdown().llm_usd > 0


def test_thinking_tokens_reach_the_tracker_from_the_provider():
    """The wiring between `RawScript` and the cost model, end to end.

    `test_cost.py` proves the arithmetic; this proves the numbers actually
    arrive. A provider that stopped reporting `thinking_tokens` would leave
    that file passing and every real job under-billed.
    """
    _, _, tracker = resolve([fixture("valid_ph.json")])
    assert tracker.totals.thinking_tokens == 500


# ---------------------------------------------------------------------------
# The committed fallbacks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["ph_scale", "covalent_bonds", "ionic_vs_covalent"])
def test_every_committed_fallback_passes_its_own_gates(key):
    """The claim the whole no-fail guarantee rests on.

    If a fallback ever stopped passing, the failure would surface at the
    renderer with no useful message instead of here.
    """
    concept = get_concept(key)
    script = load_fallback(concept)

    assert script.concept == key
    assert len(script.scenes) == 5
    assert 125 <= script.total_words <= 190


@pytest.mark.parametrize("key", ["ph_scale", "covalent_bonds", "ionic_vs_covalent"])
def test_every_fallback_is_distinct_from_the_test_fixture(key):
    """Fallbacks are written independently of the fixtures on purpose.

    If they were the same text, a degraded run and a healthy run would produce
    identical videos and only the flag would tell them apart - which makes the
    degradation impossible to see in the artifact itself.
    """
    names = {"ph_scale": "valid_ph.json",
             "covalent_bonds": "valid_covalent.json",
             "ionic_vs_covalent": "valid_comparison.json"}
    fallback = load_fallback(get_concept(key))
    reference = Script.model_validate_json(fixture(names[key]))

    overlap = {a.narration for a in fallback.scenes} & {
        b.narration for b in reference.scenes
    }
    assert not overlap, "the fallback reuses narration from the test fixture"


def test_a_missing_fallback_file_is_a_named_failure(tmp_path):
    """`script_unavailable` is a deployment error, and says so."""
    concept = get_concept("ph_scale").model_copy(
        update={"fallback_path": tmp_path / "gone.json"}
    )
    with pytest.raises(ScriptUnavailable) as caught:
        load_fallback(concept)
    assert "ph_scale" in str(caught.value)


def test_a_corrupted_fallback_is_caught_at_load_time(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text('{"concept": "ph_scale", "scenes": []}', encoding="utf-8")
    concept = get_concept("ph_scale").model_copy(
        update={"fallback_path": broken}
    )

    with pytest.raises(ScriptUnavailable) as caught:
        load_fallback(concept)
    assert "does not pass its own gates" in str(caught.value)


# ---------------------------------------------------------------------------
# Named failures reaching the job — R7
# ---------------------------------------------------------------------------

async def make_runner(generate, *, timeout_s: float = 10):
    repository = InMemoryJobRepository()
    runner = JobRunner(repository, generate, max_concurrent=1, timeout_s=timeout_s)
    job = Job.create("How does the pH scale work?")
    job.concept = "ph_scale"
    await repository.create(job)
    runner.submit(job)
    await runner.drain()
    return await repository.get(job.job_id)


async def test_a_named_stage_failure_reaches_the_job_with_its_own_code():
    """R7: failures are named. `internal_error` for everything violates it."""
    async def generate(job, report_stage):
        await report_stage("narrating")
        raise StageFailure("tts_failed", "narrating",
                           "The narration could not be produced.", "socket closed")

    job = await make_runner(generate)

    assert job.status == "failed"
    assert job.failure.code == "tts_failed"
    assert job.failure.stage == "narrating"
    assert job.failure.detail == "socket closed"


@pytest.mark.parametrize("code", ["script_unavailable", "render_failed",
                                  "mux_failed", "artifact_invalid"])
async def test_every_named_code_survives_the_runner(code):
    """Guards against half the FailureCode enum quietly becoming dead code."""
    async def generate(job, report_stage):
        raise StageFailure(code, "rendering", "nope", "detail")

    job = await make_runner(generate)
    assert job.failure.code == code


async def test_an_unexpected_exception_is_still_an_internal_error():
    """The catch-all stays. What changed is that it now means "bug"."""
    async def generate(job, report_stage):
        raise ValueError("this one really is a bug")

    job = await make_runner(generate)
    assert job.failure.code == "internal_error"
    assert job.status == "failed"


async def test_a_job_that_runs_over_budget_is_named_a_timeout():
    """A hung job is not a bug, and a client has to be able to tell.

    `code` is the machine-readable field. Reporting `internal_error` here
    would tell an automated client "the server is broken, do not retry" about
    the single most retryable failure the pipeline has. R7 asks for named
    failures, and this was the last unnamed one.
    """
    async def generate(job, report_stage):
        await report_stage("muxing")
        await asyncio.sleep(5)

    job = await make_runner(generate, timeout_s=0.05)

    assert job.status == "failed"
    assert job.failure.code == "timeout"
    assert job.failure.stage == "muxing", "the stage it hung in is preserved"
    assert "time budget" in job.failure.message
    assert "JOB_TIMEOUT_S" in job.failure.detail


# ---------------------------------------------------------------------------
# G5b's remedy is the fallback, not another TTS call
# ---------------------------------------------------------------------------

async def test_narration_that_overruns_swaps_the_script_not_the_voice(tmp_path):
    """The reason G5b exists as a separate gate at all.

    The first script narrates to 102s and blows the 90s ceiling. Re-reading the
    same words cannot help, so the orchestrator must reach for the fallback -
    whose runtime is a measured quantity - rather than retrying synthesis.
    """
    tts = FakeTTS(durations=[20.0, 20.0, 20.0, 20.0, 20.0, 12.0])
    orchestrator = Orchestrator(
        FakeLLM([fixture("valid_ph.json")]), tts,
        settings=Settings(artifact_dir=tmp_path),
    )
    outcome = ScriptOutcome(
        script=Script.model_validate_json(fixture("valid_ph.json")),
        degraded=False, attempts=1, gates=(),
    )

    async def report(stage):
        return None

    result = await orchestrator._stage_narrate(
        get_concept("ph_scale"), outcome, tmp_path, report, {}
    )

    assert result.ok
    assert 45 <= result.total_duration_s <= 90
    assert len(tts.calls) == 10, "five scenes narrated twice, not re-synthesised"


@pytest.mark.slow
async def test_a_broken_model_still_delivers_a_playable_video(tmp_path):
    """The round 8 gate, run end to end with nothing stubbed but the model.

    The LLM returns garbage on every attempt, so all three are spent and the
    fallback takes over. Everything after that point is real: edge-tts,
    matplotlib, ffmpeg, the artifact store. What this asserts is the promise
    SPEC.md §9.1 makes - a learner whose question resolved to a concept gets a
    video even when the model is completely unavailable, and the artifact says
    plainly that it was degraded.
    """
    from app.pipeline.orchestrator import Orchestrator as RealOrchestrator
    from app.providers.tts import EdgeTTSProvider
    from app.providers.visual import MatplotlibProvider
    from app.storage.artifacts import LocalArtifactStore

    settings = Settings(artifact_dir=tmp_path)
    store = LocalArtifactStore(tmp_path)
    llm = FakeLLM(["not json at all"])

    generate = RealOrchestrator(
        llm=llm,
        tts=EdgeTTSProvider(settings, cache_dir=tmp_path / "cache"),
        visual=lambda context: MatplotlibProvider(settings, context),
        store=store,
        settings=settings,
    )

    job = Job.create("How does the pH scale work?")
    job.concept = "ph_scale"
    stages: list[str] = []

    async def report(stage):
        stages.append(stage)

    artifact = await generate(job, report)

    assert llm.calls == 3, "all three attempts spent before falling back"
    assert job.degraded is True
    assert job.attempts == 3
    assert stages == ["scripting", "narrating", "rendering", "muxing", "publishing"]

    # The artifact itself, not a claim about it.
    assert 45 <= artifact.duration_s <= 90
    assert artifact.scenes == 5
    probed = ffmpeg_probe(tmp_path / job.job_id / "video.mp4")
    assert probed.has_video and probed.has_audio

    manifest = json.loads(
        (tmp_path / job.job_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["degraded"] is True
    assert manifest["attempts"] == 3
    assert [row["gate"] for row in manifest["gates"]] == ["G1", "G1", "G1"]
    assert all(row["passed"] is False for row in manifest["gates"])
    assert len(manifest["scenes"]) == 5
    assert manifest["timings"]["muxing"] > 0
    assert manifest["cost"]["production_estimate_usd"] > 0

    # Three answered calls, all of them useless, all of them billed. A
    # degraded job is not a free job: the model replied every time, it just
    # replied with garbage. Only a call that *raises* costs nothing, which is
    # why `test_a_provider_outage_still_produces_a_script` asserts zero and
    # this asserts three.
    assert manifest["tokens"]["llm_calls"] == 3
    assert manifest["cost"]["llm_usd"] > 0


# ---------------------------------------------------------------------------
# The title card — R6, found by a live run rather than by a test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["ph_scale", "covalent_bonds", "ionic_vs_covalent"])
def test_every_concept_requires_a_title_card(key):
    """CLAUDE.md §4 settles that the query is shown on the title card.

    Regression test for a real failure: a live `ionic_vs_covalent` run opened
    on `electron_transfer` instead, producing a perfectly valid video that
    never showed the learner what it was answering. The card was in
    `allowed_visuals` but nothing required it, so R6's visual proof was left
    to chance - and one run in three lost it.
    """
    assert "title_card" in get_concept(key).required_visuals


def test_a_script_with_no_title_card_is_rejected_and_retried():
    """And the feedback names the missing visual, so the retry is informed."""
    broken = json.loads(fixture("valid_comparison.json"))
    broken["scenes"][0]["visual"] = {
        "type": "electron_transfer", "params": {"donor": "Na", "acceptor": "Cl"}
    }

    outcome, llm, _ = resolve(
        [json.dumps(broken), fixture("valid_comparison.json")],
        concept_key="ionic_vs_covalent",
    )

    assert llm.calls == 2
    assert outcome.degraded is False
    assert llm.feedback[1].reason == "missing_required_visual"
    assert "title_card" in llm.feedback[1].detail


# ---------------------------------------------------------------------------
# GeminiProvider client construction
# ---------------------------------------------------------------------------

def test_the_gemini_client_is_built_once_when_threads_arrive_together():
    """A found bug, not an imagined one.

    Three jobs submitted at a freshly started server lost one attempt to
    `RuntimeError('Cannot send a request, as the client has been closed.')`.
    Two worker threads passed the `self._client is None` check together, both
    built a client, and one assignment won; the orphan was collected and
    closing its transport killed the request already in flight on it.

    `manifest.tokens.llm_calls` read 1 against `attempts` of 2 - the lost
    attempt never reached the API - and the retry loop absorbed it every
    time, which is why 30 harness runs and 338 tests never showed it. The
    harness runs sequentially by design (SPEC.md §15), so nothing ever raced
    the lazy init.
    """
    from google import genai

    from app.providers.llm import GeminiProvider

    built: list[object] = []
    barrier = threading.Barrier(8)

    class FakeClient:
        def __init__(self, api_key: str) -> None:
            # The real constructor sets up an httpx transport, which is slow
            # enough for another thread to pass the `is None` check while it
            # runs. Without this sleep the window is too narrow for the GIL to
            # interleave and the test passes with or without the lock - which
            # it did, on the first attempt at writing it.
            time.sleep(0.01)
            built.append(self)

    provider = GeminiProvider(Settings(gemini_api_key="k"))
    seen: list[object] = []

    def grab() -> None:
        barrier.wait()          # release all eight into the check at once
        seen.append(provider._get_client())

    with mock.patch.object(genai, "Client", FakeClient):
        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(built) == 1, f"{len(built)} clients built; the orphans get closed"
    assert len({id(c) for c in seen}) == 1, "threads got different clients"
