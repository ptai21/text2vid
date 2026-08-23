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
