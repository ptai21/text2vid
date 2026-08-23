"""Narration, audio-first timing and gate G5 — SPEC.md §9.2 and §10.

T2: fake providers, no network. The real edge-tts call lives in one
`@pytest.mark.slow` test at the bottom.

The claim these tests defend is that **duration is measured, never
estimated**. Everything downstream — scene timing, G5b, the final video
length — is arithmetic on a probed number, so a word-count guess anywhere in
the chain would quietly desynchronise every video.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.domain.script import Scene, Script, VisualSpec
from app.pipeline.gates import TRAILING_PAD_S, check_g5a, check_g5b
from app.pipeline.orchestrator import NarratedScene, Orchestrator
from app.providers.ffmpeg import ProbeResult
from app.providers.tts import AudioResult, EdgeTTSProvider, TTSError

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
