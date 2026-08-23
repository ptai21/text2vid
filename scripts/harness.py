"""Reliability harness — SPEC.md §15.

    uv run python -m scripts.harness --runs 5
    uv run python -m scripts.harness --runs 5 --concept ph_scale
    uv run python -m scripts.harness --runs 10 --scripts-only

Runs each concept N times and writes `reports/reliability.md`. Drives the
orchestrator in process rather than over HTTP: the question this answers is
whether *generation* is repeatable, and putting a web server in the loop would
only add a way for the measurement to fail for an unrelated reason.

**The per-gate table is the point.** Anything can look reliable once. What an
evaluator needs is the distribution, and the honest reading of it — including
gates that never fired. A guardrail that has never rejected anything across
thirty runs is either redundant or its threshold is wrong, and R8 says that
has to be stated rather than quietly enjoyed. The report says it automatically.

Two modes:

- default runs the whole pipeline, so duration and G5–G7 are real. Roughly 30s
  per run, most of it in the encoder.
- `--scripts-only` stops after the gates. Ten times faster and it isolates the
  one stage where the non-determinism actually lives, which makes it the right
  mode for a large N.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.concepts.registry import ConceptContract, all_concepts, get_concept
from app.config import Settings, get_settings
from app.domain.job import Job
from app.pipeline.cost import CostTracker
from app.pipeline.orchestrator import Orchestrator
from app.pipeline.retry import resolve_script
from app.providers import ffmpeg
from app.providers.llm import GeminiProvider
from app.providers.tts import EdgeTTSProvider
from app.providers.visual import MatplotlibProvider
from app.storage.artifacts import MANIFEST_NAME, LocalArtifactStore

REPORT_PATH = Path("reports/reliability.md")
MIN_DURATION_S = 45.0
MAX_DURATION_S = 90.0


@dataclass
class RunRecord:
    concept: str
    run: int
    ok: bool
    attempts: int = 0
    degraded: bool = False
    gates: list[dict] = field(default_factory=list)
    duration_s: float | None = None
    size_bytes: int | None = None
    cost_usd: float | None = None
    prod_usd: float | None = None
    wall_s: float = 0.0
    failure: str | None = None

    @property
    def first_attempt(self) -> bool:
        return self.ok and not self.degraded and self.attempts == 1

    @property
    def retried(self) -> bool:
        return self.ok and not self.degraded and self.attempts > 1


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

async def run_full(concept: ConceptContract, index: int, settings: Settings,
                   root: Path) -> RunRecord:
    """One complete job: model, gates, narration, frames, encode, publish."""
    store = LocalArtifactStore(root)
    generate = Orchestrator(
        llm=GeminiProvider(settings),
        tts=EdgeTTSProvider(settings, cache_dir=root / "_audio_cache"),
        visual=lambda context: MatplotlibProvider(settings, context),
        store=store,
        settings=settings,
    )

    job = Job.create(concept.canonical_question)
    job.concept = concept.key
    started = time.perf_counter()

    async def report(stage):
        return None

    try:
        artifact = await generate(job, report)
    except Exception as exc:  # noqa: BLE001 - a failed run is data, not a crash
        return RunRecord(
            concept=concept.key, run=index, ok=False,
            attempts=job.attempts, degraded=job.degraded,
            wall_s=time.perf_counter() - started,
            failure=f"{type(exc).__name__}: {exc}",
        )

    manifest = json.loads(
        (root / job.job_id / MANIFEST_NAME).read_text(encoding="utf-8")
    )
    return RunRecord(
        concept=concept.key, run=index, ok=True,
        attempts=manifest["attempts"], degraded=manifest["degraded"],
        gates=manifest["gates"],
        duration_s=artifact.duration_s, size_bytes=artifact.size_bytes,
        cost_usd=manifest["cost"]["total_usd"],
        prod_usd=manifest["cost"]["production_estimate_usd"],
        wall_s=time.perf_counter() - started,
    )


def run_scripts_only(concept: ConceptContract, index: int,
                     settings: Settings) -> RunRecord:
    """Model plus G1–G4 only. Isolates the stage that can actually vary."""
    tracker = CostTracker()
    started = time.perf_counter()

    try:
        outcome = resolve_script(
            GeminiProvider(settings), concept, tracker,
            max_attempts=settings.max_script_attempts,
        )
    except Exception as exc:  # noqa: BLE001
        return RunRecord(concept=concept.key, run=index, ok=False,
                         wall_s=time.perf_counter() - started,
                         failure=f"{type(exc).__name__}: {exc}")

    breakdown = tracker.breakdown()
    return RunRecord(
        concept=concept.key, run=index, ok=True,
        attempts=outcome.attempts, degraded=outcome.degraded,
        gates=[row.as_dict() for row in outcome.gates],
        cost_usd=breakdown.total_usd, prod_usd=breakdown.production_estimate_usd,
        wall_s=time.perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summary_rows(records: list[RunRecord]) -> list[str]:
    rows = []
    for key in sorted({record.concept for record in records}):
        runs = [record for record in records if record.concept == key]
        durations = [r.duration_s for r in runs if r.duration_s]
        costs = [r.prod_usd for r in runs if r.prod_usd]

        spread = "-"
        if durations:
            spread = (f"{min(durations):.1f} / "
                      f"{statistics.median(durations):.1f} / "
                      f"{max(durations):.1f}")

        rows.append(
            f"| `{key}` | {len(runs)} | "
            f"{sum(1 for r in runs if r.first_attempt)} | "
            f"{sum(1 for r in runs if r.retried)} | "
            f"{sum(1 for r in runs if r.degraded)} | "
            f"{sum(1 for r in runs if not r.ok)} | "
            f"{spread} | "
            f"{('$%.4f' % statistics.mean(costs)) if costs else '-'} |"
        )
    return rows


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def gate_table(records: list[RunRecord]) -> tuple[list[str], list[str]]:
    """Per-gate failure counts, plus the honest reading of the zeroes."""
    fired: Counter[str] = Counter()
    reasons: Counter[str] = Counter()

    for record in records:
        for row in record.gates:
            if not row["passed"]:
                fired[row["gate"]] += 1
                reasons[row.get("reason", "").split(":")[0] or "unknown"] += 1

    # G5-G7 do not appear in the manifest gate log: they either pass or they
    # end the job, so a run that completed proves they passed.
    tracked = ["G1", "G2", "G3", "G4"]
    rows = [f"| {gate} | {fired.get(gate, 0)} |" for gate in tracked]

    silent = [gate for gate in tracked if not fired.get(gate)]
    notes: list[str] = []
    if silent:
        notes.append(
            f"**{', '.join(silent)} did not fire once across "
            f"{plural(len(records), 'run')}.** "
            "R8 asks that guardrails earn their place, so the honest reading is "
            "one of three: the model is comfortably inside the constraint, the "
            "threshold is too loose to bind, or the gate is redundant. The "
            "prompt is built from the same registry the gates check, so the "
            "first is the expected outcome — but a gate that never fires is "
            "only justified by the cost of the failure it prevents, and that "
            "case has to be made in words rather than inferred from a zero."
        )
    if reasons:
        top = ", ".join(f"`{reason}` ×{count}"
                        for reason, count in reasons.most_common(5))
        notes.append(f"Most common rejection reasons: {top}.")
    return rows, notes


def acceptance(records: list[RunRecord], *, full: bool) -> list[str]:
    failed = [r for r in records if not r.ok]
    completed = [r for r in records if r.ok]
    durations = [r.duration_s for r in records if r.duration_s]
    outside = [d for d in durations if not MIN_DURATION_S <= d <= MAX_DURATION_S]
    degraded = sum(1 for r in records if r.degraded)

    def mark(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    lines = [
        f"| 1 | No `failed` jobs | {mark(not failed)} | "
        f"{len(failed)} of {plural(len(records), 'run')} failed |",
        # Criterion 2 is about the videos that exist, not the runs that did
        # not produce one - criterion 1 already counts those. A job cannot
        # reach `completed` without passing G7, so this is a restatement of
        # the completed count rather than an independent measurement, and it
        # is written that way instead of implying a separate check ran.
        f"| 2 | Every completed video passes G7 | "
        f"{('PASS' if completed else 'n/a') if full else 'n/a'} | "
        + (f"{len(completed)} completed runs; a job cannot reach `completed` "
           "without G7 passing" if full
           else "not exercised in --scripts-only mode") + " |",
        "| 3 | Every script satisfies its concept anchors | PASS | "
        "by construction: G4 gates it, or the fallback is used |",
        f"| 4 | Duration inside {MIN_DURATION_S:.0f}-{MAX_DURATION_S:.0f}s | "
        f"{mark(not outside) if durations else 'n/a'} | "
        + (f"{len(outside)} of {len(durations)} outside the window"
           if durations else "no videos produced in this mode") + " |",
        f"| 5 | Degraded rate reported, not hidden | PASS | "
        f"{degraded} of {len(records)} runs used the fallback |",
    ]
    return lines


def build_report(records: list[RunRecord], *, full: bool, runs: int,
                 model: str, elapsed: float) -> str:
    failed = [r for r in records if not r.ok]
    lines: list[str] = [
        "# Reliability report",
        "",
        "Generated by `scripts/harness.py`. Do not edit by hand — re-run the",
        "harness instead, or the numbers stop meaning anything.",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Mode: {'full pipeline' if full else 'scripts only (no TTS, no encode)'}",
        f"- Runs per concept: {runs}",
        f"- Total runs: {len(records)}",
        f"- Model: `{model}`",
        f"- Wall time: {elapsed / 60:.1f} min",
        "",
        "## Per concept",
        "",
        "| Concept | Runs | First-attempt pass | Needed retry | Degraded | "
        "Failed | Duration min/med/max | Mean cost |",
        "|---|---|---|---|---|---|---|---|",
        *summary_rows(records),
        "",
        "`Mean cost` is the production estimate from SPEC.md §14, not what the",
        "prototype spent — edge-tts is free and the Gemini free tier bills",
        "nothing, so actual spend is dominated by a number that would be zero.",
        "",
    ] + ([] if full else [
        "**In `--scripts-only` mode that figure is not a whole-job cost.** No",
        "narration was synthesised, so the TTS line — which SPEC.md §14 puts at",
        "65-75% of a real job — is absent. Only the model call and the flat",
        "per-job infrastructure are counted here.",
        "",
    ]) + [
        "## Gate failures",
        "",
        "| Gate | Times it rejected a script |",
        "|---|---|",
    ]

    rows, notes = gate_table(records)
    lines += rows + [""]
    for note in notes:
        lines += [note, ""]

    lines += [
        "G5, G6 and G7 are absent from this table by construction: they do not",
        "produce a retry, they end the job. Every completed run above is",
        "therefore a run in which all three passed.",
        "",
        "## Acceptance criteria — SPEC.md §15",
        "",
        "| # | Criterion | Result | Detail |",
        "|---|---|---|---|",
        *acceptance(records, full=full),
        "",
    ]

    if failed:
        lines += ["## Failures", ""]
        for record in failed:
            lines.append(f"- `{record.concept}` run {record.run}: {record.failure}")
        lines.append("")

    lines += [
        "## Every run",
        "",
        "| Concept | Run | Attempts | Degraded | Duration | Prod cost | Wall |",
        "|---|---|---|---|---|---|---|",
    ]
    for record in records:
        duration = f"{record.duration_s:.1f}s" if record.duration_s else "-"
        cost = f"${record.prod_usd:.4f}" if record.prod_usd else "-"
        status = "yes" if record.degraded else "no"
        if not record.ok:
            status = "FAILED"
        lines.append(
            f"| `{record.concept}` | {record.run} | {record.attempts} | "
            f"{status} | {duration} | {cost} | {record.wall_s:.1f}s |"
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------

async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5,
                        help="runs per concept; SPEC section 15 asks for at least 5")
    parser.add_argument("--concept", choices=[c.key for c in all_concepts()])
    parser.add_argument("--scripts-only", action="store_true",
                        help="stop after the gates: no TTS, no render, no encode")
    parser.add_argument("--delay", type=float, default=4.0,
                        help="seconds between runs; the free tier is 5-15 RPM")
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    parser.add_argument("--artifacts", type=Path,
                        default=Path("./artifacts/_harness"))
    args = parser.parse_args()

    settings = get_settings()
    if not settings.gemini_api_key:
        raise SystemExit("GEMINI_API_KEY is not set - see SETUP.md section 2")
    if not args.scripts_only and not ffmpeg.available():
        raise SystemExit("ffmpeg or ffprobe not on PATH - see SETUP.md section 1")

    concepts = ([get_concept(args.concept)] if args.concept
                else list(all_concepts()))
    args.artifacts.mkdir(parents=True, exist_ok=True)

    records: list[RunRecord] = []
    total = len(concepts) * args.runs
    started = time.perf_counter()

    for concept in concepts:
        for index in range(1, args.runs + 1):
            position = len(records) + 1
            print(f"[{position}/{total}] {concept.key} run {index} ... ",
                  end="", flush=True)

            if args.scripts_only:
                record = run_scripts_only(concept, index, settings)
            else:
                record = await run_full(concept, index, settings, args.artifacts)
            records.append(record)

            if not record.ok:
                print(f"FAILED  {record.failure}")
            else:
                flag = " degraded" if record.degraded else ""
                length = f" {record.duration_s:.1f}s" if record.duration_s else ""
                print(f"ok  attempts={record.attempts}{flag}{length}  "
                      f"{record.wall_s:.1f}s")

            # Sequential and throttled on purpose: at 5-15 RPM a burst is how
            # the harness manufactures a quota failure and then reports it as
            # unreliability (SPEC.md §15).
            if position < total and args.delay > 0:
                await asyncio.sleep(args.delay)

    elapsed = time.perf_counter() - started
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        build_report(records, full=not args.scripts_only, runs=args.runs,
                     model=settings.gemini_model, elapsed=elapsed),
        encoding="utf-8",
    )

    failed = [r for r in records if not r.ok]
    degraded = sum(1 for r in records if r.degraded)
    print(f"\n{len(records)} runs in {elapsed / 60:.1f} min - "
          f"{len(failed)} failed, {degraded} degraded")
    print(f"report written to {args.out.resolve()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
