# Execution record — plan versus actual

What `PLAN.md` said to build, what was actually built, and every place the two
came apart. Kept separate from [findings.md](findings.md): that file records
*defects*, this one records *divergence*. A plan that matched reality perfectly
would either be a very good plan or a record nobody checked.

Read alongside `git log` — 23 commits, each carrying the command it was gated on
and the output that command produced.

---

## 1. What exists

| | |
|---|---|
| Application | 4,616 lines across `app/` |
| Tests | 3,369 lines — **338 fast** (~11s, offline, quota-free) + **12 slow** |
| Scripts | 1,087 lines — `smoke`, `demo`, `render_demo`, `harness` |
| Documentation | 2,707 lines across 9 files |
| Commits | 23, Conventional Commits, each with an observed `Gate:`/`Result:` |
| Deliverables | 3 videos, each with `script.json`, `manifest.json`, `query.txt` |

Every one of the ten rounds completed and was committed. No round failed its
gate twice; the rollback rule in `PLAN.md` was never invoked.

---

## 2. Round by round

| Round | Planned Touches | Also touched | Why |
|---|---|---|---|
| 1 Skeleton | `pyproject`, `main`, `config`, `logging` | — | matched |
| 2 Domain | `app/domain/*`, `test_state` | — | matched |
| 3 Concepts | `app/concepts/*`, `test_resolver` | — | matched |
| 4 API + runner | `app/api/*`, `app/storage/*`, `runner.py`, `test_api` | `main.py`, `stub.py`, `providers/ffmpeg.py`, `SPEC.md` | The round needed a stub generator and something to wire it into; neither was listed. ffmpeg because the stub writes a **real** MP4, not an inert blob |
| 5 LLM + G1–G4 | `llm.py`, `gates.py`, `prompts/*`, `test_prompt` | — | matched |
| 6 TTS + G5 | `tts.py`, `orchestrator.py`, `test_pipeline` | `gates.py` | G5 belongs with G1–G7; `gates.py` was in no round's list after round 5 |
| 7 Renderer + G6/G7 | `visual`, `theme`, `scenes/*`, `ffmpeg` | `gates.py`, `registry.py`, **`scripts/render_demo.py`**, **`tests/test_render.py`** | G6/G7 same reason as G5. `registry.py` for the `title` field the title card needed. Two new files — see §3 |
| 8 Retry + manifest | `retry`, `cost`, `fallbacks/*`, `test_pipeline` | `orchestrator.py`, `main.py`, `runner.py`, `registry.py`, `visual.py` | The round's own gate was unobservable until the pipeline replaced the stub in `main.py`. `runner.py` for `StageFailure` — see §4 |
| 9 Harness | `scripts/harness.py`, `reports/` | — | matched |
| 10 Deliverables | `README`, `ARCHITECTURE`, `submissions/`, `test_contracts` | `domain/job.py`, `runner.py`, `api/routes.py`, `providers/ffmpeg.py`, `providers/visual.py`, `SPEC.md`, `demo.py`, 3 test files | Three separate approved changes — see §4 |

**Every expansion above was escalated before the edit, never after.** That is the
rule in `CLAUDE.md` §5, and it held for all ten rounds.

---

## 3. Built although no document specified it

Four things exist that neither `PLAN.md` nor `CLAUDE.md` §8 listed.

| What | Why it had to exist |
|---|---|
| `scripts/render_demo.py` | Round 7's `Verify` was `scripts.demo`, which needs the pipeline wired to the API — round 8's work. Round 7 had no way to verify itself. This runs fixture → real TTS → render → mux → mp4 with **no server and no Gemini quota**, and its `--corrupt` mode is how G7 is proven to reject a broken artifact |
| `tests/test_render.py` | Round 7 had no sanctioned test file: `test_gates.py` is pre-written and forbidden to edit, `test_pipeline.py` belongs to round 8 |
| `reports/findings.md` | Nothing in the plan recorded mistakes. The brief grades *AI-agent workflow*, and a build with no record of its own errors cannot evidence one |
| `reports/execution.md` | This file |

`tests/test_contracts.py` is a fifth case with a different shape: `CLAUDE.md` §8
**named** it as the T3 slow suite, but no round's Touches ever claimed it. It sat
unassigned through nine rounds before being folded into round 10 by decision. It
found a live gate failure on its first run.

---

## 4. Contract changes made after `SPEC.md` was written

Four. Each was raised, decided, then implemented — spec first, code second.

| Change | Reason | Blast radius |
|---|---|---|
| `JobRepository` made async | `InMemoryJobRepository` needs an `asyncio.Lock`; a real one would do I/O anyway. `ArtifactStore` stays sync, deliberately | SPEC §12 |
| `StageFailure` carries a `FailureCode` | `runner.py` hardcoded `internal_error`, leaving five of nine codes as dead code and making R7 true only on paper | `runner.py`, round 8 |
| `timeout` added to `FailureCode` | A hung job reported `internal_error` — the code reserved for bugs — telling an automated client not to retry the *most* retryable failure there is | SPEC §3, domain enum, 2 tests |
| `GET /videos/{job_id}/script` | SPEC §12 declares a three-file bundle; only two were reachable over HTTP. The missing one is the output of the sole non-deterministic stage | SPEC §5, `routes.py`, `demo.py`, 3 tests |

A fifth was found after round 10, by probing the running service rather than by
reading code: `SPEC.md` §5 says *"Every non-2xx uses one shape"*, but Starlette answers
an unknown path and an unrecognised verb **in the router**, before any route function
runs. Those two replies never reached `APIError` and came back as `{"detail": ...}`.
Fixed with one handler, and the spec sentence is now a parametrised test across all
four handlers plus the two the router raises alone.

Plus one **rewrite** rather than an addition: `SPEC.md` §7/§9.3/§9.4/§13 were
rewritten before round 7 because the spec implied per-scene animation (~900
`savefig` calls per video) while `PLAN.md` said five. Five won.

---

## 5. The one plan cut that was taken

`PLAN.md` Part 3 ranks what to drop if time runs out. Cut #1 is
*"`zoompan` subtle zoom → plain stills"*.

It was taken — but **not for time**. The zoom trembled, and the arithmetic says
it always would have: `zoompan` crops in whole input pixels, and a 4% ramp over a
12-second scene moves the crop origin 0.136px per frame, so it holds for seven
frames, jumps one, holds for eight, jumps again. That uneven rhythm is what the
eye reads as shake. It cannot be tuned out — one pixel per frame needs a 14.6×
source, and a 2× source needs a 39% zoom that would crop the captions off.

Removing it also cut encode time by 2.2× and file size by 2.25×. Encoding was
77% of wall time, so this is the largest single performance change in the build.

Cuts #2–#5 (crossfades, harness runs, `/concepts`, audio caching) were **not**
taken. Nothing on the "never cut" line was touched.

---

## 6. Scope discipline

`CLAUDE.md` §2 lists sixteen things not to build. Verified absent:

- No `Dockerfile`, no `docker-compose.yml`, no `.github/`, no `alembic.ini`
- No `celery`, `redis`, `sqlalchemy`, `alembic`, `slowapi`, `passlib` or
  `python-jose` import anywhere in `app/`
- No frontend, no auth, no cache layer, no webhooks, no cancel endpoint, no
  range requests, no fourth concept

Dependency containment (`CLAUDE.md` §8) also holds: `subprocess`,
`google.genai` and `edge_tts` are imported **only** in `app/providers/`. Two
files outside it mention those names — `cost.py` and `stub.py` — and in both
cases the match is a comment, not a call.

Dependencies were never added without approval. `pyproject.toml` carries nine
runtime packages and three dev packages, all present from round 0.

---

## 7. Where the plan was simply wrong

Not divergence by choice — places the plan could not have worked as written.

1. **Round 7 could not verify itself.** Its `Verify` command depended on round 8.
2. **Round 8's gate was unobservable.** *"With an always-failing LLM the job still
   completes with `degraded=true`"* — but the API still ran `StubGenerator`, so
   there was no LLM in the path to fail.
3. **Round 7 had no test file it was allowed to write to.**
4. **`gates.py` was orphaned after round 5**, while `CLAUDE.md` §8 says it holds
   G1–**G7**. Two later rounds had to reclaim it.
5. **`tests/test_contracts.py` and `app/domain/script.py` were named in the
   layout but owned by no round.**
6. **`CLAUDE.md` §7 budgets the fast suite at "under ~3s".** It runs in ~11s, of which
   **3.5s is collection** — importing matplotlib, FastAPI and `google-genai`. The
   slowest single test is 0.39s and the eight slowest together are ~2s, so this is not
   slow tests, it is import cost. The budget was written before round 1, and matplotlib
   arrived in round 7; it has been unreachable ever since. Recorded here rather than
   edited into `CLAUDE.md`, whose timestamps are evidence (§9 of that file).

The pattern is consistent: `PLAN.md` allocated *implementation* files carefully
and *verification* files barely at all. Every one of these was found by trying to
run the round's own gate rather than by re-reading the plan.

---

## 8. What the evidence actually shows

| Claim | How it is evidenced | Strength |
|---|---|---|
| Generation is repeatable | **30** harness runs across two passes: 0 failed, 0 degraded, every duration in 45–90s | Strong |
| The retry loop is load-bearing | **9** gate rejections in the harness plus more in contract tests and demo runs — every one recovered, none degraded, including one script that needed all three attempts | Strong |
| The scripting stage cannot fail | `test_a_broken_model_still_delivers_a_playable_video` — garbage on all 3 attempts, still a 59.6s video with both streams | Strong |
| G7 stops a broken artifact | `render_demo --corrupt` rejects a truncated encode, a dropped audio track and a drifted duration; passes the intact file | Strong |
| Cost is metered, retries included | `test_cost.py` (16 tests) + live manifests; real spend $0.0317 across 15 runs | Strong |
| Named failures reach the API | Parametrised over every stage code through the real runner | Strong |
| Boundaries are swappable | Protocols exist and are the only import path — but only one implementation of each was ever built | **Asserted, not proven** |
| Visual quality | Judged by eye across 8 rendered stills and several videos | Subjective |

The two weak rows are stated as weak on purpose. A second `TTSProvider` would
move the first from asserted to proven; nothing in scope required one.

---

## 9. What the second harness pass changed

The harness was run twice: once before the encoder zoom came out, once after. The
second pass is not a repeat — it changed three conclusions.

| | Pass 1 | Pass 2 |
|---|---|---|
| First-attempt pass | 13 / 15 | **9 / 15** |
| Gate rejections | 2 | **7** |
| Gates that fired | G2 only | G2 ×6, **G3 ×1** |
| Wall time | 14.0 min | **7.8 min** |
| Failed · degraded | 0 · 0 | 0 · 0 |

1. **G3 fired for the first time** (`param_out_of_range`). Until then the README had to
   justify it from history — the empty-`params` bug — rather than from the counter. It
   now has a production catch.
2. **`G2/total_words` became the most common rejection**, four times, always low. Pass 1
   produced none at all. That is a direct lesson about sample size: the conclusion drawn
   from fifteen runs was not wrong, it was underpowered.
3. **Wall time nearly halved**, confirming that removing the zoom was the single largest
   performance change in the build.

The uncomfortable reading is the useful one: the pass with the *worse* first-attempt rate
is the stronger evidence. Nine rejections absorbed with nothing degraded says more about
reliability than fifteen clean runs ever could.

---

## 10. Outstanding

| | Owner |
|---|---|
| Demo walkthrough covering all three concepts | you |
| Grant GitHub read access to the three evaluator addresses (pushed) | you |
| Upload the working-session recording, verify the share link plays | you |
| Zip excluding `.venv/`, `__pycache__/`, `artifacts/`, `.git/` and **`.env`** | you |
| Visual repetition (`max_repeats_per_visual`) — deliberately **not** gated on one observation | open, low |
| `MAX_TOTAL_WORDS` left at 190 — the ceiling never bound; the model undershoots instead | closed, watch only |
