"""Round 7 verification — script to watchable MP4, without the API.

    uv run python -m scripts.render_demo
    uv run python -m scripts.render_demo --concept ph_scale
    uv run python -m scripts.render_demo --corrupt

Drives the second half of the pipeline directly: a gated reference script in,
real edge-tts narration, five rendered stills, G6, the crossfade mux, ffprobe
and G7 out. No server, no Gemini call, no quota - so the visual design can be
iterated on in a tight loop, which is the whole reason this exists alongside
`scripts.demo`.

`scripts.demo` remains the end-to-end walkthrough over HTTP once the real
generator is wired to the API in round 8. This one deliberately stops short of
that: it proves the *renderer and encoder*, and nothing it touches can fail for
a reason that lives in the web layer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from app.concepts.registry import all_concepts, get_concept
from app.config import get_settings
from app.domain.script import Script
from app.pipeline.gates import check_g6, check_g7
from app.pipeline.orchestrator import Orchestrator
from app.providers import ffmpeg
from app.providers.scenes import RenderContext
from app.providers.tts import EdgeTTSProvider
from app.providers.visual import MatplotlibProvider

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "llm"
REFERENCE = {
    "ph_scale": "valid_ph.json",
    "covalent_bonds": "valid_covalent.json",
    "ionic_vs_covalent": "valid_comparison.json",
}


def step(label: str, detail: str = "") -> None:
    print(f"  {label:<14}{detail}")


async def render_one(key: str, out_root: Path) -> dict:
    settings = get_settings()
    concept = get_concept(key)
    script = Script.model_validate(
        json.loads((FIXTURES / REFERENCE[key]).read_text(encoding="utf-8"))
    )

    work = out_root / key
    work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    # -- narrate: durations are measured here and drive everything after it --
    tts = EdgeTTSProvider(settings, cache_dir=out_root / "_audio_cache")
    narration = await Orchestrator(llm=None, tts=tts).narrate(script, work / "audio")
    if not narration.ok:
        step("FAILED", f"{narration.failure.gate} {narration.failure.reason}")
        return {"key": key, "ok": False, "failure": narration.failure}

    clips = [round(scene.duration_s, 1) for scene in narration.scenes]
    step("narrated", f"{clips}  total {narration.total_duration_s:.1f}s")

    # -- render ----------------------------------------------------------
    context = RenderContext(query=concept.canonical_question,
                            concept_title=concept.title)
    visual = MatplotlibProvider(settings, context)
    drawn = time.perf_counter()
    frames: list[Path] = []
    for scene in narration.scenes:
        frames += visual.render(scene.scene, scene.display_duration_s,
                                work / "frames")
    step("rendered", f"{len(frames)} stills in "
                     f"{(time.perf_counter() - drawn) * 1000:.0f}ms")

    g6 = check_g6(frames, expected=len(script.scenes),
                  width=settings.video_width, height=settings.video_height)
    if g6 is not None:
        step("G6 FAILED", f"{g6.reason}: {g6.detail}")
        return {"key": key, "ok": False, "failure": g6}
    step("G6", "pass")

    # -- mux -------------------------------------------------------------
    video = work / "video.mp4"
    encoded = time.perf_counter()
    ffmpeg.mux(
        [
            ffmpeg.MuxScene(still=frame, audio=scene.path,
                            duration_s=scene.display_duration_s)
            for frame, scene in zip(frames, narration.scenes)
        ],
        video,
        width=settings.video_width, height=settings.video_height,
        fps=settings.video_fps,
    )
    step("muxed", f"{time.perf_counter() - encoded:.1f}s")

    probed = ffmpeg.probe(video)
    g7 = check_g7(probed, expected_duration_s=narration.total_duration_s)
    if g7 is not None:
        step("G7 FAILED", f"{g7.reason}: {g7.detail}")
        return {"key": key, "ok": False, "failure": g7}
    step("G7", f"pass  video={probed.has_video} audio={probed.has_audio} "
               f"{probed.duration_s:.1f}s {probed.size_bytes / 1e6:.1f}MB")

    return {
        "key": key, "ok": True, "path": video,
        "expected_s": narration.total_duration_s,
        "actual_s": probed.duration_s,
        "size_mb": probed.size_bytes / 1e6,
        "wall": time.perf_counter() - started,
    }


def prove_g7_rejects_corruption(out_root: Path) -> bool:
    """Break a finished video three ways and confirm G7 refuses each.

    Part of the round 7 gate. A validation gate nobody has watched fail is an
    assertion, not a guarantee - and this is the gate standing between a broken
    file and a job reported as `completed`.

    The three cases are the three ways this pipeline can actually produce a bad
    artifact: a truncated encode, a mux that silently dropped the narration
    track, and crossfade arithmetic that drifted. The middle one matters most,
    because a silent slideshow is the specific failure R5 names.
    """
    source = next(out_root.glob("*/video.mp4"), None)
    if source is None:
        print("no video to corrupt - run without --corrupt first")
        return False

    good = ffmpeg.probe(source)
    print()
    print(f"source {source.parent.name}/video.mp4  "
          f"{good.duration_s:.1f}s  {good.size_bytes / 1e6:.1f}MB")

    truncated = out_root / "corrupt_truncated.mp4"
    truncated.write_bytes(source.read_bytes()[: 150 * 1024])

    silent = out_root / "corrupt_silent.mp4"
    ffmpeg.run(["-i", str(source), "-an", "-c:v", "copy", str(silent)])

    cases = [
        ("truncated encode", truncated, good.duration_s),
        ("narration track dropped", silent, good.duration_s),
        ("crossfade drift", source, good.duration_s + 8.0),
    ]

    ok = True
    for label, path, expected in cases:
        try:
            probed = ffmpeg.probe(path)
        except ffmpeg.FFmpegError as exc:
            print(f"  {label:<26} ffprobe rejected it outright: {exc}")
            continue

        failure = check_g7(probed, expected_duration_s=expected)
        if failure is None:
            print(f"  {label:<26} G7 ACCEPTED IT - this is a bug")
            ok = False
            continue
        print(f"  {label:<26} G7 rejected: {failure.reason}")

    # The same gate must pass the file it is meant to pass, or "it rejects
    # everything" would read as success above.
    if check_g7(good, expected_duration_s=good.duration_s) is not None:
        print("  intact artifact          G7 REJECTED a good file - this is a bug")
        ok = False
    else:
        print("  intact artifact            G7 pass")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concept", choices=sorted(REFERENCE))
    parser.add_argument("--out", default=Path("./artifacts/_demo"), type=Path)
    parser.add_argument("--corrupt", action="store_true",
                        help="prove G7 rejects a truncated artifact, then exit")
    args = parser.parse_args()

    if not ffmpeg.available():
        raise SystemExit("ffmpeg or ffprobe not on PATH - see SETUP.md section 1")

    if args.corrupt:
        return 0 if prove_g7_rejects_corruption(args.out) else 1

    keys = [args.concept] if args.concept else [c.key for c in all_concepts()]
    results = []
    for key in keys:
        print(f"\n[{key}]")
        results.append(await render_one(key, args.out))

    print("\n" + "=" * 72)
    print(f"{'concept':<22}{'expected':<11}{'actual':<10}{'MB':<7}{'wall'}")
    print("-" * 72)
    for row in results:
        if not row["ok"]:
            print(f"{row['key']:<22}FAILED  {row['failure'].reason}")
            continue
        print(f"{row['key']:<22}{row['expected_s']:<11.1f}{row['actual_s']:<10.1f}"
              f"{row['size_mb']:<7.1f}{row['wall']:.1f}s")
    print("=" * 72)

    failed = [r for r in results if not r["ok"]]
    if failed:
        print(f"\n{len(failed)} of {len(results)} failed")
        return 1
    print(f"\nall {len(results)} rendered - open them under {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
