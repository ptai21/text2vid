# Findings

Everything that went wrong, everything the documents disagreed about, and
every decision that was escalated rather than guessed. Kept because the brief
grades **AI-agent workflow** — visible planning, verifiable steps, inspected
output — and a build that never records its own mistakes cannot evidence that.

Covers rounds 1–10.

---

## 1. Bugs found

Ordered by how much they mattered, not by when they happened. The **Caught by**
column is the interesting one.

### Would have shipped broken

| # | Bug | Caught by | Fix |
|---|---|---|---|
| 1 | `response_schema` declared `params` as a bare `{"type": "object"}`. Gemini's constrained generation emits **only declared properties**, so `params` came back empty and all three live calls failed `G3/missing_param`. | First real Gemini call | `VISUAL_PARAM_TYPES`, narrowed per concept, + 2 regression tests |
| 2 | `title_card` was in `allowed_visuals` but in no concept's `required_visuals`. A live `ionic_vs_covalent` run opened on `electron_transfer` and produced a valid video that **never showed the learner their own question** — breaking R6 and the settled decision in CLAUDE.md §4. | Reading a real `manifest.json` | `required_visuals=["title_card"]` on all three concepts |
| 3 | Marking `completed` for a file with no audio stream was possible until G7 existed. | SPEC §10 review | G7 checks both streams, size and duration drift |

The zoom is the sharpest of these. The 2× upscale was supposed to fix exactly
the stutter that appeared, the code comment said so confidently, and the
arithmetic was never checked — 2× halved the amplitude and changed nothing
about the cause. **A mitigation reasoned correctly and never measured.** It took
watching a finished video, then computing the per-frame crop origin, to see it.

**Both #1 and #2 were invisible to the test suite.** All 325 tests were green
before and after each. #1 could not be caught by a unit test because every
fixture already had `params` populated — only the model's own output omitted
them. #2 produced output that passed all seven gates.

That is the single most useful lesson from this build: **the tests prove the
rules are enforced; only looking at the artifact proves the rules are right.**

### Real, caught earlier

| # | Bug | Caught by | Fix |
|---|---|---|---|
| 4 | G4's anchor matcher used `(?<!\w)term(?!\w)`, so `"hydrogen ion"` did not match `"hydrogen ions"` and G4 named the wrong missing anchor. | Failing gate test | Suffix-tolerant `\w*` match |
| 5 | The model wrote `"seven"` where the anchor expected `"7"` — a G4 false negative costing a whole extra LLM call. | Live run | Fixed in **both** places: anchor accepts either, prompt asks for digits |
| 6 | Two non-JSON lines at startup: uvicorn logged before `lifespan` ran `configure_logging`. | Piping stdout through a JSON parser | Moved into `create_app()` so it runs at import |
| 7 | `LocalArtifactStore.write_text` was not in the SPEC §12 Protocol, so the stub depended on the concrete class — quietly voiding the "swapping is a one-class change" claim. | Self-review against SPEC | Removed; publishing writes a temp file and calls `put()` |
| 8 | `ModuleNotFoundError: app.main` reported as an environment problem. It was not — the file had simply never been written. | Reading the traceback instead of the error | Round 1 |

### Visual defects — none of which any test could see

Found by rendering the PNGs and looking at them.

| # | Defect | Fix |
|---|---|---|
| 9 | `ph_scale_bar`: markers at 0 and 14 sliced in half by the axes edge; marker values collided with the band labels. | Four fixed vertical zones, `xlim` padded to ±0.7 |
| 10 | `log_steps`: on a linear axis the 1× bar is one percent of the 100× bar and **vanishes** — teaching nothing, in the scene whose whole job is the logarithm. | Log axis: bar tops form an even staircase, every "×10" arrow the same length |
| 11 | `atom_pair` / `electron_transfer`: `set_aspect()` turned the electron shells into ellipses overflowing the frame. | `theme.circle()` — pre-computes the content zone's 3.111 aspect, because `set_aspect(1)` resizes the axes box and breaks the shared layout grid |
| 12 | Title card showed the concept title three times over. | Dropped the redundant footer, rebalanced vertically |
| 13 | **The finished video trembled.** The subtle `zoompan` 1.00→1.04 moved its crop origin 0.136px per frame, so the crop held for seven frames, jumped one pixel, held for eight, jumped again. The *unevenness* is what the eye catches — a steady drift would have been invisible. | Removed the zoom. It is not tunable: 1px/frame needs a 14.6× source (18720×10530), and a 2× source needs a 39% zoom that would crop the captions off. A subtle zoom and `zoompan` are incompatible by construction. Encode got **2.2× faster** as a side effect |

### Bugs the tests could not have found — the pattern, stated once

Three of the four most serious problems in this build were invisible to a green
test suite, and all three were found by looking at something real: a live
response, a rendered PNG, a `manifest.json`. `tests/test_contracts.py` (round
10) exists because of that pattern — it is the only suite that can fail for a
reason nobody anticipated. It earned its place on its **first run**, producing a
115-word script that the 15-run harness never saw (open issue 1).

### Mistakes in my own tests, not in the code

Recorded because "the test failed" and "the code is wrong" are different
things, and conflating them is how a correct guard gets deleted.

| # | What happened | Reality |
|---|---|---|
| 13 | Two tests used 1-scene scripts and failed G5b. | The code was right — a 12s total genuinely violates the 45s floor. Rewrote with 5-scene scripts. |
| 14 | Asserted `manifest.tokens.llm_calls == 0` on the degraded end-to-end run. | Wrong. The model **answered** three times; it answered with garbage. Tokens are still billed. Only a call that *raises* costs nothing. A degraded job is not a free job. |
| 15 | `test_contracts.py` asserted every scene comes back with populated `params`. | Wrong. `title_card` declares no params by design — its text is injected by the renderer from `RenderContext`, which is the same mechanism that keeps the learner's query out of the prompt. Scoped the assertion to visuals that actually declare params. |
| 15b | Fixing the timeout code turned `tests/test_api.py::test_job_timeout_is_enforced` red — it asserted `internal_error`. | **Not** a case of editing a test to make code pass, and worth spelling out because the two look identical from the outside. Round 4 wrote that line because `FailureCode` had no `timeout` member yet, so the runner had nothing else to report; SPEC §3 has always defined `internal_error` as *unhandled*, and a timeout is handled in a named `except` block. The assertion recorded an accident, not a contract. The spec row changed first, then the test. `test_api.py` is round 4's file, not pre-written — checked before editing. |
| 16 | `test_contracts.py` asserted a single live response clears every gate. | Flaky by construction. The harness measured ~1 first-attempt rejection in 8, and the design says that is unremarkable. Rewrote it around `resolve_script` — the contract is that the **loop converges**, not that the model is perfect. `degraded is True` is the real alarm. |

---

## 2. Document contradictions

Each of these was escalated rather than guessed, per the standing instruction.
`CLAUDE.md` = what is allowed · `SPEC.md` = correct behaviour · `PLAN.md` =
what to do next.

| # | Conflict | Resolution |
|---|---|---|
| 1 | Scene count and word budget: PLAN said exactly 5 scenes / 25–38 / 125–190 words; SPEC §7 said 4–6 scenes / 25–60. | PLAN won. SPEC §7 and §10 updated. |
| 2 | SPEC §12 gave `JobRepository` sync signatures, but `InMemoryJobRepository` needs an `asyncio.Lock`. | Made async. SPEC updated with the reason. `ArtifactStore` stays sync. |
| 3 | CLAUDE.md §8 says `gates.py` holds G1–**G7**, but `gates.py` was outside the Touches list of both round 6 (G5) and round 7 (G6/G7). | All gates in `gates.py`. Touches expanded with approval, twice. |
| 4 | Round 7 motion: SPEC implied per-scene animation (~900 `savefig` calls); PLAN said five. | Five. SPEC §7/§9.3/§9.4/§13 rewritten; all motion from the encoder. |
| 5 | Crossfade length: 0.3s in one place, 0.4s in another. | 0.4s. |
| 6 | Round 7's Verify was `scripts.demo`, which needs the pipeline wired to the API — work belonging to round 8. | New `scripts/render_demo.py`: fixture → real TTS → render → mux → mp4, no server, **no Gemini quota**. |
| 7 | SPEC §7 says the title card shows a "concept title" injected by the renderer. `ConceptContract` had no such field, and `canonical_question` would have printed the learner's question twice. | Added `title` to the registry. |
| 8 | Round 7 had no sanctioned test file: `test_gates.py` is pre-written and forbidden to edit, `test_pipeline.py` belongs to round 8. | New `tests/test_render.py`. |
| 9 | Round 8's Gate ("the job still completes with `degraded=true`") is unobservable — the API still ran `StubGenerator`. | Wired the real orchestrator; `main.py` swapped in one place. |
| 10 | `runner.py` hardcoded `internal_error` for every failure, leaving 5 of 9 `FailureCode` values as dead code and violating R7. | `StageFailure` carries the code; the catch-all stays and now genuinely means "bug". |
| 11 | Gemini 2.0 Flash was shut down 2026-06-01. | Moved to `gemini-3.5-flash-lite`; SPEC §14 repriced at $0.30/$2.50 per 1M with thinking tokens billed as output. |

---

## 3. Gaps in `PLAN.md`'s Touches lists

A recurring pattern rather than isolated slips: files named in CLAUDE.md §8
that no round claimed.

| File | Status |
|---|---|
| `app/domain/script.py` | Resolved — folded into round 2, which owns `app/domain/*` |
| `tests/test_cost.py` | Resolved — made pre-written, registered in PLAN §0.1 and round 8 |
| `tests/test_contracts.py` | **Resolved.** Assigned to round 10 by decision and built: 8 slow tests against real Gemini, edge-tts and ffprobe. Found a live `G2/total_words` case on its first run. |
| Round 7 | Missed `gates.py`, any test file, and a verification vehicle |
| Round 8 | Missed `orchestrator.py`, `main.py`, `runner.py` |

---

## 4. Open issues

| # | Issue | Severity | Note |
|---|---|---|---|
| 1 | **G2's word budget binds at the *floor*, not the ceiling.** The ceiling worry was wrong: across 15 harness runs the longest video was 78.5s against 90s. But the first live call from `test_contracts.py` produced **115 words against the 125 minimum** — `G2/total_words` firing low. | Closed, understood | The model undershoots, it does not run long. `MIN_TOTAL_WORDS` is doing real work and `MAX_TOTAL_WORDS` stays at 190 — narrowing a threshold that has never bound would be a speculative edit dressed as a fix. Note the harness never saw this: 15 runs missed a case the very first contract run hit. |
| 2 | `tests/test_contracts.py` has no owning round. | Medium | **Resolved by decision:** built in round 10 as the T3 slow suite. |
| 11 | ~~**A job timeout reports `internal_error`.**~~ | Closed | `JobRunner` caught `asyncio.TimeoutError` and called `_fail` without a code, taking the `internal_error` default reserved for bugs. The message, detail and log line were all correct — only the machine-readable `code` was wrong, which is why nothing failed. Fixed: `"timeout"` added to `FailureCode`, runner passes it, SPEC §3 row added, one test. Escalated first, since `app/domain/job.py` was outside round 10's Touches. **Found by fact-checking the README against the code**, which is a review pass worth keeping — writing documentation is the only step that reads the whole surface at once. |
| 3 | Visual polish still wanted. | Low | Deferred by request; no specifics given yet. `render_demo` iterates without touching quota. |
| 4 | Encoding dominates wall time — ~21s of ~28s per job. | Low | A 15-run harness spends ~5 min in ffmpeg alone. `--scripts-only` exists for that reason. |
| 5 | Thinking tokens were **0** on every live call — now across 17 harness calls plus the 4 before it. | Closed | Settled: `gemini-3.5-flash-lite` under a `response_schema` does not think. SPEC §14's upper bound is pessimistic for this workload, and the cost model still bills them correctly if they ever appear. Real spend across all 15 runs: **$0.0317**. |
| 6 | `edge-tts` is an unofficial endpoint. | Known, accepted | Mitigated by explicit timeout, 3× backoff, sha256 audio cache, and the `TTSProvider` seam. Must be stated in README. |
| 7 | Gemini free tier: 5–15 RPM, ~1,000–1,500/day, and free-tier data may be used to improve Google's products. | Known, accepted | Fine for a prototype, not for real learner data. Must be stated in README. |
| 8 | ~~The harness has not been run.~~ | Closed | 15 runs, 0 failed, 0 degraded, all 5 SPEC §15 criteria PASS. See `reports/reliability.md`. |
| 9 | **Encoding is 77% of wall time** — muxing averaged 35.4s of a ~46s job (scripting 6.9s, narrating 8.6s, rendering 1.3s). | Improved, still the largest stage | Removing the zoom (defect 13) took a rebuilt 63.3s `ph_scale` video's mux to **11.5s** and its file from ~3.3MB to 1.5MB. The harness figures above predate that change; its reliability figures — durations, costs, gate counts — are audio- and gate-driven and unaffected. |
| 11b | **`script.json` was unreachable over HTTP.** SPEC §12 declares a three-file bundle; the API exposed only the video and the manifest, so `scripts/demo.py` could not assemble a complete submission without reaching past the API onto local disk. | Closed | Added `GET /videos/{job_id}/script` — an API contract change, escalated first. The withheld file was the output of the *only* non-deterministic stage, and on a degraded run the only way to see which fallback was served. Found by checking the produced `submissions/` against PLAN Part 2 rather than against the code. |
| 12b | **A single visual can fill a video.** One `ionic_vs_covalent` run chose `side_by_side_comparison` for 4 of 5 scenes. Every gate passed: it is in `allowed_visuals`, it satisfies `required_visuals`, and no rule caps repetition. | Open, low — **not** gated | Legal and coherent, just monotonous. A `max_repeats_per_visual` rule in G3 would forbid it, and adding one on a single observation is exactly what R8 warns against. The next run of the same concept came back with 4 distinct visuals unprompted, which says the model varies on its own and the sample was the problem, not the prompt. Handled by **selection** — the brief asks for the three *best* runs alongside the distribution, so picking the varied one is the sanctioned move and costs no guardrail. Revisit only if repetition shows up across a harness pass. |
| 10 | `ionic_vs_covalent` is measurably the hardest concept: 3/5 first-attempt (vs 5/5 for the other two), longest durations, highest cost. | Low | Both G2 rejections in the whole run landed here. Two `VisualType`s and a comparison table make it the most constrained prompt. Worth stating in the README rather than averaging away. |

---

## 5. Decisions escalated to you

Eighteen points where the documents did not settle it and guessing would have
been a rebuild. Recorded so the reasoning is auditable, not just the outcome.

**Contract shape** — exactly 5 scenes for G2 · async `JobRepository` · all 7
endpoints built in round 4 · real ffmpeg MP4 for the round-4 stub rather than
an inert blob.

**Where code lives** — G5 in `gates.py` · G6/G7 in `gates.py` · `main.py` and
`stub.py` outside round 4's Touches · `orchestrator.py` + `main.py` outside
round 8's · `runner.py` on top of that.

**Correctness calls** — relax the anchor matcher for plural forms · fix
`"seven"` in both the anchor *and* the prompt rather than either alone ·
require `title_card` immediately rather than waiting for harness data.

**Round 7** — five renders per video, not ~900 · 0.4s crossfades · verify via
a new `render_demo.py` · add `title` to the registry · new
`tests/test_render.py`.

**Round 8** — wire the pipeline to the API now · write fallback scripts fresh
instead of reusing the test fixtures, so a degraded run is visibly different
from a healthy one.

---

## 6. What is proven, and how

| Claim | Evidence |
|---|---|
| The scripting stage cannot fail | `test_a_broken_model_still_delivers_a_playable_video` — garbage on all 3 attempts, still a 59.6s video with both streams |
| Retries are informed, not repeated | `test_a_failed_gate_is_retried_with_feedback_naming_it` — attempt 2 receives the gate and the missing anchor |
| Fallbacks are usable, not decorative | All three pass G1–G4 and were measured through real edge-tts: 59.6s / 53.3s / 55.8s |
| The video is exactly as long as its narration | Three full runs: expected 63.3/60.4/68.5s, actual 63.3/60.5/68.5s |
| G7 stops a broken artifact | `render_demo --corrupt` — rejects a truncated encode, a dropped audio track and a drifted duration, and passes the intact file |
| Named failures reach the API | Parametrised over all five stage codes through the real runner |
| Cost is metered, retries included | `test_cost.py` (16 tests) + a live manifest showing tokens, calls and both totals |

### What the 15-run harness added

| Claim | Evidence |
|---|---|
| Generation is *repeatable*, not lucky once | 15/15 completed, 0 failed, 0 degraded, every duration inside 45–90s |
| The retry loop is load-bearing, not decoration | `G2/narration_length` rejected 2 scripts; both retries passed and neither job degraded. Without the retry those two are fallbacks |
| Cost is stable, not a lucky sample | $0.0151–$0.0231 production estimate per video; spread is 1.5×, not an order of magnitude |
| The fallback is a real path, not a hope | Still 0 uses in production conditions — proven only by `test_a_broken_model_still_delivers_a_playable_video`, which is the correct way to prove it |

**Still not proven:** anything about a fourth concept, a different model, or
sustained load. Fifteen runs is a distribution, not a guarantee — and R8's
question about G1/G3/G4 is now answered in words rather than by a zero
(§ the gate table in `reports/reliability.md`).
