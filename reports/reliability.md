# Reliability report

> **Status: the harness has not been run yet.**
>
> This file is a placeholder. `scripts/harness.py` **overwrites it** with
> generated numbers. Everything below is either methodology, or a measurement
> that was actually taken during development — each one labelled with how it
> was obtained. Nothing here is estimated, extrapolated, or filled in by hand
> to look complete.
>
> To produce the real report:
>
> ```bash
> uv run python -m scripts.harness --runs 5
> ```

---

## What the harness measures

`scripts/harness.py` runs each of the three concepts N times and writes the
table SPEC.md §15 asks for: first-attempt pass rate, retries needed, degraded
rate, failures, duration spread, and mean cost — plus **per-gate failure
counts**.

The per-gate table is the part that matters. Anything looks reliable once.
What an evaluator needs is the distribution, and the honest reading of it —
including gates that never fired. R8 says a guardrail has to earn its place,
so the report states the zeroes explicitly rather than quietly enjoying them.

It drives the orchestrator **in process**, not over HTTP. The question is
whether generation is repeatable; putting a web server in the loop would only
add a way for the measurement to fail for an unrelated reason.

### Two modes

| Mode | What runs | Cost per run | Use it for |
|---|---|---|---|
| default | full pipeline: model, gates, TTS, render, encode, publish | ~30s, mostly encoding | duration and G5–G7 are real |
| `--scripts-only` | model + G1–G4 only | ~3s | large N against the one stage that actually varies |

Runs are **sequential and throttled** (`--delay`, default 4s). The Gemini free
tier is 5–15 RPM; a burst is how a harness manufactures a quota failure and
then reports it as unreliability.

### Expected wall time

Three concepts × 5 runs = 15 runs. At ~30s each plus throttling, roughly
**10–13 minutes**. Run it in the background, not during a screen recording.

---

## Measurements already taken

These are real, but they are **not** a substitute for the harness: each is a
single observation or a handful, not a distribution.

### Live API runs, real Gemini — `scripts.demo`

Three concepts, one pass, through the running FastAPI service.

| Concept | Attempts | Degraded | Duration | Size | Production cost |
|---|---|---|---|---|---|
| `ph_scale` | 1 | no | 59.6s | 2.9 MB | $0.0161 |
| `covalent_bonds` | 1 | no | 61.2s | 3.3 MB | $0.0172 |
| `ionic_vs_covalent` | 1 | no | 79.6s | 4.4 MB | $0.0219 |

Plus one further live run after the `title_card` fix: `ionic_vs_covalent`,
1 attempt, 72.1s, $0.0199.

All four calls passed G1–G4 on the **first attempt**. No retries, no
fallbacks. That is four data points, which is why the harness exists.

### Reference scripts through real edge-tts

The three gate-passing fixtures, narrated and measured with ffprobe:

| Script | Words | Clips (s) | Total | Implied wpm |
|---|---|---|---|---|
| `valid_ph` | 150 | 10.9, 12.3, 12.5, 13.8, 11.8 | 63.3s | 147 |
| `valid_covalent` | 149 | 12.1, 11.0, 12.0, 11.4, 12.0 | 60.4s | 153 |
| `valid_comparison` | 145 | 12.4, 13.9, 13.4, 13.5, 13.2 | 68.5s | 131 |

### Committed fallback scripts

| Fallback | Words | Total | Implied wpm |
|---|---|---|---|
| `ph_scale` | 138 | 59.6s | 139 |
| `covalent_bonds` | 137 | 53.3s | 154 |
| `ionic_vs_covalent` | 139 | 55.8s | 150 |

All three inside 45–90s with margin. This is what makes the "the scripting
stage cannot fail" claim in SPEC.md §9.1 more than an assertion.

### Degraded path, end to end

`tests/test_pipeline.py::test_a_broken_model_still_delivers_a_playable_video`
gives the model a response that cannot parse, on every attempt:

```
llm.calls = 3, attempts = 3, degraded = True
video 59.6s, both streams present
manifest.gates = [G1 false, G1 false, G1 false]
manifest.tokens.llm_calls = 3
```

Three answered calls, all useless, **all billed**. A degraded job is not a
free job — only a call that raises costs nothing.

---

## Open observations to check against the distribution

Things a single pass cannot settle. The harness should either confirm or kill
each of these.

**1. The G2 word budget has almost no headroom at the top.**
190 words (the G2 ceiling) at 131 wpm (the slowest pace observed) is 87.0s,
plus 5 × 0.4s trailing pads = **89.0s against a 90s ceiling**. The lower bound
is comfortable: 125 words at 153 wpm ≈ 51s against a 45s floor. The live
`ionic_vs_covalent` run at 79.6s is the closest anything has come. If the
harness produces a G5b failure, this is why, and the fix is lowering
`MAX_TOTAL_WORDS` to ~175 rather than widening the duration window.

**2. Thinking tokens were zero on every live call.**
SPEC.md §14 prices a range spanning up to one thinking token per visible
output token, and `scripts/smoke.py` once observed ~500. Constrained JSON
generation appears not to need them. The cost model bills them correctly when
they appear — the point is that the §14 *upper* bound may be pessimistic for
this workload, and observed cost ($0.0161–$0.0219) already sits at or below
the documented $0.021–0.025 band.

**3. Which gates actually fire is unknown.**
Across four live calls, none did. If G1–G3 stay at zero across 15 runs, the
report will say so and the README has to make the case for them in words —
that a cheap check preventing an expensive failure is justified even when it
never fires — rather than implying they were load-bearing.

---

## Acceptance criteria — SPEC.md §15

The harness evaluates these and writes the results into this file.

| # | Criterion |
|---|---|
| 1 | `failed` count = 0 across all runs (infrastructure failures excepted and reported) |
| 2 | Every completed video passes G7 |
| 3 | Every completed script satisfies all its concept's anchors |
| 4 | Duration always within 45–90s |
| 5 | `degraded` rate reported, not hidden |

Criterion 3 holds by construction rather than by measurement: G4 gates it, or
the fallback is used, and the fallback passes G4 too. The report says that
plainly instead of presenting it as an independent check.
