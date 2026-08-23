"""Environment smoke test.

Verifies the four external dependencies independently, so a failure points at one
thing rather than at a tangle. Run before the build session:

    uv run python -m scripts.smoke

Check 1 also prints the resolved google-genai call signature, which is the highest-risk
unknown going into the session (see SPEC.md section 8).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="t2v_smoke_"))
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    status = "ok  " if ok else "FAIL"
    print(f"[{len(RESULTS)}/4] {name:.<18} {status} ({detail})")


def load_env() -> None:
    env = Path(".env")
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------- 1. gemini

def check_gemini() -> None:
    started = time.perf_counter()
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        record("gemini", False, f"import failed: {exc}")
        return

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        record("gemini", False, "GEMINI_API_KEY missing from .env")
        return

    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    schema = {
        "type": "object",
        "properties": {
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "string"},
                        "narration": {"type": "string"},
                    },
                    "required": ["scene_id", "narration"],
                },
            }
        },
        "required": ["scenes"],
    }

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents="Return exactly two scenes about water. Narration under 15 words each.",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.4,
            ),
        )
        payload = json.loads(response.text)
        count = len(payload.get("scenes", []))
        if count < 1:
            record("gemini", False, "valid JSON but no scenes returned")
            return
        elapsed = time.perf_counter() - started
        record("gemini", True, f"{model}, {count} scenes, {elapsed:.1f}s")

        print()
        print("    --- confirm this against SPEC.md section 8 ---")
        print(f"    client        : {type(client).__module__}.{type(client).__name__}")
        print("    call          : client.models.generate_content(model=, contents=, config=)")
        print("    config type   : google.genai.types.GenerateContentConfig")
        print("    schema kwarg  : response_schema (+ response_mime_type='application/json')")
        print("    text accessor : response.text")
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            print(f"    usage         : {usage}")
        print()
    except Exception as exc:  # noqa: BLE001 - smoke test reports, does not handle
        record("gemini", False, f"{type(exc).__name__}: {exc}")


# -------------------------------------------------------------- 2. edge-tts

def check_edge_tts() -> Path | None:
    started = time.perf_counter()
    out = TMP / "narration.mp3"
    voice = os.environ.get("TTS_VOICE", "en-US-AriaNeural")
    text = (
        "The pH scale measures how acidic or basic a solution is, "
        "running from zero to fourteen."
    )
    try:
        import asyncio

        import edge_tts

        async def synth() -> None:
            communicate = edge_tts.Communicate(text, voice)
            await asyncio.wait_for(communicate.save(str(out)), timeout=30)

        asyncio.run(synth())
    except Exception as exc:  # noqa: BLE001
        record("edge-tts", False, f"{type(exc).__name__}: {exc}")
        return None

    if not out.exists() or out.stat().st_size == 0:
        record("edge-tts", False, "produced an empty file")
        return None

    duration = probe_duration(out)
    if duration is None or duration < 1.0:
        record("edge-tts", False, f"audio too short ({duration})")
        return None

    elapsed = time.perf_counter() - started
    record(
        "edge-tts",
        True,
        f"{duration:.1f}s audio, {out.stat().st_size // 1024}KB, {elapsed:.1f}s",
    )
    return out


# ------------------------------------------------------------ 3. matplotlib

def check_matplotlib() -> Path | None:
    out = TMP / "frame.png"
    try:
        import matplotlib

        matplotlib.use("Agg")  # must precede pyplot; an interactive backend hangs
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=(12.8, 7.2), dpi=100)
        ax = fig.add_subplot(111)
        ax.barh([0], [14], color="#7F77DD")
        ax.set_xlim(0, 14)
        ax.set_yticks([])
        ax.set_title("pH scale", fontsize=22)
        fig.savefig(out, facecolor="white")
        plt.close(fig)
    except Exception as exc:  # noqa: BLE001
        record("matplotlib", False, f"{type(exc).__name__}: {exc}")
        return None

    if not out.exists() or out.stat().st_size == 0:
        record("matplotlib", False, "no png written")
        return None

    record("matplotlib", True, "1280x720 png")
    return out


# ----------------------------------------------------------------- 4. ffmpeg

def probe_duration(path: Path) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def probe_streams(path: Path) -> set[str]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def check_ffmpeg(png: Path | None, mp3: Path | None) -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            record("ffmpeg mux", False, f"{tool} not on PATH (check from Git Bash)")
            return

    if png is None or mp3 is None:
        record("ffmpeg mux", False, "skipped: needs checks 2 and 3 to pass first")
        return

    out = TMP / "smoke.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(png),
        "-i", str(mp3),
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no stderr"]
        record("ffmpeg mux", False, tail[0][:120])
        return

    streams = probe_streams(out)
    if not {"video", "audio"} <= streams:
        record("ffmpeg mux", False, f"missing stream, found {sorted(streams)}")
        return

    duration = probe_duration(out) or 0.0
    record("ffmpeg mux", True, f"{duration:.1f}s mp4, video+audio streams")


# -------------------------------------------------------------------- main

def main() -> int:
    load_env()
    print(f"scratch: {TMP}\n")

    check_gemini()
    mp3 = check_edge_tts()
    png = check_matplotlib()
    check_ffmpeg(png, mp3)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        print("Fix these before starting the build session. See SETUP.md section 10.")
        return 1

    print("all checks passed")
    print(f"artifacts left in {TMP} - open smoke.mp4 to confirm it plays with sound")
    return 0


if __name__ == "__main__":
    sys.exit(main())
