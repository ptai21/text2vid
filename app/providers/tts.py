"""Narration synthesis — SPEC.md §9.2 and §11.

`edge-tts` is free and needs no API key, which is what makes the reliability
harness affordable. It is also an **unofficial endpoint** with no default
timeout, so every call is wrapped in one — a hang here would otherwise sit
inside `JOB_TIMEOUT_S` doing nothing (SETUP.md §10).

Duration is **measured with ffprobe, never estimated from word count**. That
is the whole basis of audio-first timing: visuals are cut to the audio, so
sync is a consequence rather than something to calibrate (CLAUDE.md §4).

TTS failure gets a different retry policy from the LLM because it is a
*network* failure: 3 attempts with exponential backoff, per scene. On
exhaustion the job fails with `tts_failed` — a video without narration would
violate R5, so there is no sensible fallback here.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from collections.abc import Awaitable, Callable

from app.config import Settings
from app.logging import get_logger
from app.providers import ffmpeg

Speak = Callable[[str, str, str, Path], Awaitable[None]]
Probe = Callable[[Path], "ffmpeg.ProbeResult"]

log = get_logger(__name__)

TTS_TIMEOUT_S = 30.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
CACHE_DIRNAME = "_audio_cache"


class TTSError(RuntimeError):
    """Exhausted retries. Maps to the `tts_failed` FailureCode."""


@dataclass(frozen=True)
class AudioResult:
    path: Path
    duration_s: float
    """Probed, not estimated."""
    size_bytes: int
    cached: bool = False


class TTSProvider(Protocol):
    async def synthesize(self, text: str, out: Path) -> AudioResult: ...


class EdgeTTSProvider:
    """Caches by `sha256(text + voice + rate)`.

    SPEC.md §14 identifies audio caching as the highest-leverage cost lever in
    production — TTS is the largest line item — and during a harness run it is
    what stops fifteen passes re-synthesising identical narration.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
        timeout_s: float = TTS_TIMEOUT_S,
        speak: Speak | None = None,
        probe: Probe | None = None,
    ):
        self._voice = settings.tts_voice
        self._rate = settings.tts_rate
        self._cache_dir = cache_dir or (settings.artifact_dir / CACHE_DIRNAME)
        self._backoff = backoff
        self._timeout_s = timeout_s
        # The network call and the prober are injectable so the cache and
        # retry logic can be exercised without a socket. They are real seams,
        # not a way to mock the class under test: what is replaced is the
        # boundary, and the caching and backoff being tested stay real.
        self._speak_impl = speak or self._edge_tts_speak
        self._probe = probe or ffmpeg.probe

    def cache_key(self, text: str) -> str:
        digest = hashlib.sha256(
            f"{text}\x00{self._voice}\x00{self._rate}".encode("utf-8")
        )
        return digest.hexdigest()

    def _cached_path(self, text: str) -> Path:
        return self._cache_dir / f"{self.cache_key(text)}.mp3"

    async def synthesize(self, text: str, out: Path) -> AudioResult:
        out.parent.mkdir(parents=True, exist_ok=True)
        cached = self._cached_path(text)

        if cached.is_file() and cached.stat().st_size > 0:
            shutil.copyfile(cached, out)
            return self._describe(out, cached=True)

        await self._synthesize_with_retries(text, out)

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, cached)
        return self._describe(out, cached=False)

    async def _synthesize_with_retries(self, text: str, out: Path) -> None:
        attempts = len(self._backoff)
        last: Exception | None = None

        for attempt, delay in enumerate(self._backoff, start=1):
            try:
                await asyncio.wait_for(self._speak(text, out), timeout=self._timeout_s)
                if out.is_file() and out.stat().st_size > 0:
                    return
                last = TTSError("edge-tts produced an empty file")
            except asyncio.TimeoutError as exc:
                last = TTSError(f"edge-tts timed out after {self._timeout_s}s")
                last.__cause__ = exc
            except Exception as exc:  # noqa: BLE001 - network provider
                last = exc

            log.warning("tts.attempt_failed", attempt=attempt, of=attempts,
                        error=repr(last))
            if attempt < attempts:
                await asyncio.sleep(delay)

        raise TTSError(f"TTS failed after {attempts} attempts: {last!r}") from last

    async def _edge_tts_speak(self, text: str, voice: str, rate: str,
                              out: Path) -> None:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(out))

    async def _speak(self, text: str, out: Path) -> None:
        await self._speak_impl(text, self._voice, self._rate, out)

    def _describe(self, path: Path, *, cached: bool) -> AudioResult:
        probed = self._probe(path)
        return AudioResult(
            path=path,
            duration_s=probed.duration_s,
            size_bytes=path.stat().st_size,
            cached=cached,
        )
