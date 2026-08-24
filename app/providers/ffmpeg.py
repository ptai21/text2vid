"""ffmpeg / ffprobe access — the only place either binary is invoked.

CLAUDE.md §8: no `subprocess` call to ffmpeg anywhere else. Round 4 needs
just enough of this to produce and inspect a placeholder artifact; round 7
adds the real mux, crossfades and loudnorm on top.

`stderr` is captured on every call and attached to `FFmpegError`. ffmpeg
reports what went wrong exclusively on stderr, so discarding it is how a mux
failure becomes "ffmpeg exited 1" with no explanation (SETUP.md §10).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

FFMPEG_TIMEOUT_S = 120.0
FFPROBE_TIMEOUT_S = 30.0


class FFmpegError(RuntimeError):
    """Carries stderr, which becomes `failure.detail`."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def available() -> bool:
    """Both binaries, not just ffmpeg.

    G5 and G7 depend on ffprobe specifically, and installing ffmpeg without
    ffprobe is a common and confusing failure (SETUP.md §1).
    """
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(args: list[str], *, timeout: float = FFMPEG_TIMEOUT_S) -> str:
    """Run ffmpeg with the given arguments. Returns stderr on success."""
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited {completed.returncode}", completed.stderr
        )
    return completed.stderr


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    size_bytes: int
    has_video: bool
    has_audio: bool


def probe(path: Path) -> ProbeResult:
    """Measured facts about a media file. G5 and G7 both read this.

    Duration comes from here and never from a word count — audio-first timing
    is the whole reason sync is a consequence rather than a calibration task
    (CLAUDE.md §4).
    """
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_S,
    )
    if completed.returncode != 0:
        raise FFmpegError(
            f"ffprobe exited {completed.returncode}", completed.stderr
        )

    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams", [])
    container = payload.get("format", {})

    return ProbeResult(
        duration_s=float(container.get("duration", 0.0) or 0.0),
        size_bytes=int(container.get("size", 0) or 0),
        has_video=any(s.get("codec_type") == "video" for s in streams),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def encode_placeholder(
    out: Path, *, seconds: float, width: int, height: int, fps: int
) -> None:
    """A real, playable MP4 with both a video and an audio stream.

    Round 4 drives the lifecycle to `completed` before any AI exists. Writing
    an inert byte blob would mean reporting success for a file with no audio
    stream, which CLAUDE.md §11 forbids outright — so the placeholder is a
    genuine, if uninteresting, video.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    run([
        "-f", "lavfi", "-i", f"color=c=0x101820:s={width}x{height}:r={fps}",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
        "-t", str(seconds),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ])


# ---------------------------------------------------------------------------
# Muxing — SPEC.md §9.4
# ---------------------------------------------------------------------------

CROSSFADE_S = 0.4
# There is no zoom. `zoompan` crops in whole input pixels, and a 4% ramp over a
# 12-second scene moves its crop origin 0.136px per frame - so the crop holds
# for seven frames, jumps one pixel, holds for eight, jumps again. That uneven
# rhythm is what the eye reads as trembling, and no upscale fixes it: reaching
# one pixel per frame needs a 14.6x source (18720x10530), while keeping a 2x
# source needs a 39% zoom, which would crop 28% off every edge and cut the
# captions in half. A subtle zoom and `zoompan` are incompatible by
# construction. Removing it also cut encode time by 2.2x. PLAN.md Part 3
# already ranked this the first thing to cut.

CRF = 23
PRESET = "veryfast"
AUDIO_BITRATE = "128k"
SAMPLE_RATE = 48000
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


@dataclass(frozen=True)
class MuxScene:
    still: Path
    audio: Path
    duration_s: float
    """On-screen duration: measured narration plus the trailing pad."""


def still_durations(durations: Sequence[float],
                    *, crossfade: float = CROSSFADE_S) -> list[float]:
    """How long each still must be held so the crossfades come out even.

    A crossfade *consumes* time: N stills joined by N-1 transitions of length
    T run for `sum - (N-1)*T`, not `sum`. Left uncorrected the finished video
    would be 1.6s shorter than the narration it carries, and every scene after
    the first would drift out of sync with its own audio.

    Each still after the first is therefore held T longer than its audio. The
    extra is exactly what the transition eats, so the total lands back on the
    measured narration length and G7's duration check passes on arithmetic
    rather than on tolerance.
    """
    if not durations:
        return []
    return [durations[0]] + [d + crossfade for d in durations[1:]]


def xfade_offsets(stills: Sequence[float],
                  *, crossfade: float = CROSSFADE_S) -> list[float]:
    """When each transition starts, in the *output* timeline.

    Chained `xfade` offsets accumulate against an output that is already
    shrinking, hence the `- k*T` term. Combined with `still_durations` above,
    transition k lands exactly on scene k's trailing silence: the picture has
    finished changing by the moment the next scene starts speaking.
    """
    offsets: list[float] = []
    running = 0.0
    for index, duration in enumerate(stills[:-1], start=1):
        running += duration
        offsets.append(round(running - index * crossfade, 4))
    return offsets


def _video_chain(index: int, width: int, height: int, fps: int) -> str:
    """One still to one constant-rate video stream. `settb` so xfade can join them."""
    return (
        f"[{index}:v]"
        f"scale={width}:{height}:flags=bicubic,setsar=1,fps={fps},"
        f"format=yuv420p,settb=AVTB[v{index}]"
    )


def build_filtergraph(scenes: Sequence[MuxScene], *, width: int, height: int,
                      fps: int, crossfade: float = CROSSFADE_S) -> tuple[str, str]:
    """Returns the filter description and the label its video output carries."""
    count = len(scenes)
    stills = still_durations([s.duration_s for s in scenes], crossfade=crossfade)
    offsets = xfade_offsets(stills, crossfade=crossfade)

    parts = [_video_chain(index, width, height, fps) for index in range(count)]

    current = "v0"
    for index, offset in enumerate(offsets, start=1):
        nxt = f"x{index}"
        parts.append(
            f"[{current}][v{index}]"
            f"xfade=transition=fade:duration={crossfade}:offset={offset}[{nxt}]"
        )
        current = nxt

    # Audio inputs follow the stills, so scene k's mp3 is input count+k.
    for index in range(count):
        parts.append(
            f"[{count + index}:a]"
            f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
            f"apad=pad_dur={crossfade}[a{index}]"
        )

    if count > 1:
        joined = "".join(f"[a{i}]" for i in range(count))
        parts.append(f"{joined}concat=n={count}:v=0:a=1[araw]")
        audio_in = "araw"
    else:
        audio_in = "a0"

    parts.append(f"[{audio_in}]{LOUDNORM}[aout]")
    return ";".join(parts), current


def mux(scenes: Sequence[MuxScene], out: Path, *, width: int, height: int,
        fps: int, crossfade: float = CROSSFADE_S) -> None:
    """Stills plus narration to a single MP4 — one ffmpeg invocation.

    One process rather than per-scene segments and a concat pass: there is a
    single exit code and a single stderr to attach to a failure, and no
    intermediate files to leave behind when it fails.
    """
    if not scenes:
        raise FFmpegError("mux called with no scenes")

    out.parent.mkdir(parents=True, exist_ok=True)
    stills = still_durations([s.duration_s for s in scenes], crossfade=crossfade)

    args: list[str] = []
    for scene, duration in zip(scenes, stills):
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.4f}",
                 "-i", str(scene.still)]
    for scene in scenes:
        args += ["-i", str(scene.audio)]

    graph, video_out = build_filtergraph(scenes, width=width, height=height,
                                         fps=fps, crossfade=crossfade)

    args += [
        "-filter_complex", graph,
        "-map", f"[{video_out}]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(SAMPLE_RATE),
        "-movflags", "+faststart",
        str(out),
    ]
    run(args)
