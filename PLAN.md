# PLAN.md

Build order. What to do next, in what order, and how each step is verified.
Behaviour is in `SPEC.md`; permissions in `CLAUDE.md`; environment in `SETUP.md`.

**Format:** one recorded session, approximately 3.5–4.5 hours with two breaks.

---

## Part 0 — Before recording

All of Part 0 happens off camera. Debugging ffmpeg on video costs recording time and
demonstrates nothing about your engineering.

1. Complete `SETUP.md` §1–§3.
2. `uv run python -m scripts.smoke` — all four checks green.
3. Confirm the `google-genai` call signature printed by check 1 against `SPEC.md` §8.
4. Write `tests/test_gates.py` and the fixture corpus (§0.1 below).
5. Commit the docs (§0.2).
6. `SETUP.md` §9 pre-flight checklist.

### 0.1 Pre-written gate tests

`tests/test_gates.py` and `tests/fixtures/llm/` derive directly from `SPEC.md` §6 and
§10. They need no agent, and having them **red** at the start of round 5 is what makes
that round worth recording.

`tests/test_cost.py` is pre-written on the same terms, from `SPEC.md` §14. It is pure
arithmetic with no I/O, and it pins the one thing a cost model on a thinking model gets
wrong: thinking tokens bill at the output rate and never appear in `response.text`.

```
valid_ph.json  valid_covalent.json  valid_comparison.json
g1_not_json.txt  g1_missing_field.json  g1_wrong_type.json
g2_four_scenes.json  g2_six_scenes.json  g2_empty_narration.json
g2_total_words_low.json  g2_total_words_high.json
g3_unknown_visual_type.json  g3_missing_param.json  g3_param_out_of_range.json
g4_covalent_no_energy.json        <- the most valuable fixture in the repo
g4_comparison_ionic_only.json  g4_ph_no_logarithm.json  g4_cross_contamination.json
```

Capture broken fixtures from real high-temperature runs where you can. An observed
failure is more convincing than an invented one — and you can say so on camera.

Name tests so they read as documentation:

```
test_g2_rejects_script_whose_total_word_count_would_exceed_ninety_seconds
test_g4_rejects_covalent_script_missing_energy_rationale
test_g4_rejects_comparison_that_only_explains_ionic
test_pipeline_falls_back_and_flags_degraded_after_two_failed_retries
test_job_never_reaches_completed_when_artifact_has_no_audio_stream
```

### 0.2 Documentation commit

```
docs: add agent rules, system spec, build plan and gate test corpus
```

These timestamps are the evidence for the brief's *"before and during implementation,
show your thinking clearly."* Never rewrite them.

---

## Part 1 — Session structure

| Block | Rounds | Approx | Ends with |
|---|---|---|---|
| **A** | 1–4 | 70 min | Full job lifecycle working against a stub generator |
| *break* | | 10 min | |
| **B** | 5–6 | 65 min | Real scripts, all gates passing, narration synthesised |
| *break* | | 10 min | |
| **C** | 7–8 | 80 min | Watchable MP4, retry/fallback proven, manifest emitted |
| **D** | 9–10 | 45 min | Harness running in background while docs are written |

The order is chosen so that **stopping at any block boundary leaves a demonstrable
system**. If time or energy runs out, you still have something coherent to submit.

Each round has a fixed shape:

```
Goal        one sentence
Touches     files the agent may modify - nothing else
Depends on  SPEC sections
Verify      exact command
Gate        machine-checkable pass condition
Commit      conventional-commit subject
```

**Rollback rule.** A round that fails its gate twice stops. Revert to the previous
commit and re-approach. Do not stack fixes on a broken round — that is how a session
becomes unrecoverable, and it reads badly on camera.

---

## Block A — lifecycle

### Round 1 — Skeleton, config, health · 10 min

**Goal** App boots, config loads from `.env`, `/health` reports ffmpeg availability.
**Touches** `pyproject.toml`, `app/main.py`, `app/config.py`, `app/logging.py`
**Depends on** SPEC §5
**Verify** `uv run uvicorn app.main:app` then `curl localhost:8000/health`
**Gate** `200 {"status":"ok","ffmpeg":true,"queue_depth":0}`, structured JSON logs on stdout
**Commit** `feat(api): bootstrap FastAPI app with config and health check`

### Round 2 — Domain model and state machine · 20 min

**Goal** `Job`, enums, transition table, guard rejecting illegal moves.
**Touches** `app/domain/*`, `tests/test_state.py`
**Depends on** SPEC §3, §4
**Verify** `uv run pytest tests/test_state.py -q`
**Gate** Illegal transitions raise `InvalidTransition`; `completed` unreachable without an
artifact; terminal states never re-transition
**Commit** `feat(domain): add job model and state machine`

Pure Python, no I/O. The natural first TDD round.

### Round 3 — Concept registry and resolver · 15 min

**Goal** Three concept contracts as data; deterministic query → `ConceptKey`.
**Touches** `app/concepts/*`, `tests/test_resolver.py`
**Depends on** SPEC §6
**Verify** `uv run pytest tests/test_resolver.py -q`
**Gate** Canonical questions and paraphrases resolve; out-of-scope → `unsupported_concept`;
multi-match → `ambiguous_query`; length bounds → `invalid_request`
**Commit** `feat(concepts): add concept registry and rule-based resolver`

### Round 4 — API and async runner (stub generation) · 25 min

**Goal** All endpoints working end to end against a stub generator that writes a
placeholder artifact. Proves the lifecycle before any AI is involved.
**Touches** `app/api/*`, `app/storage/*`, `app/pipeline/runner.py`, `tests/test_api.py`
**Depends on** SPEC §5, §12
**Verify** `uv run pytest tests/test_api.py -q`
**Gate** `POST` → 202; list and detail work; artifact 409 while running, 200 once complete;
uniform error envelope; semaphore caps concurrency at 2; **an exception escaping the task
sets `failed` + `internal_error` rather than leaving the job in `running`**; `JOB_TIMEOUT_S`
enforced
**Commit** `feat(api): add video job endpoints with async runner`

Separating lifecycle from AI is deliberate: when generation later misbehaves, the job
machinery is already known-good, so failures are unambiguous.

> **Checkpoint A.** `uv run python -m scripts.demo` completes all three concepts with
> placeholder artifacts. The lifecycle is demonstrable.

---

## Block B — script and narration

### Round 5 — LLM provider, prompts, gates G1–G4 · 40 min

**Goal** Script generation with all four gates. The core of the reliability story.
**Touches** `app/providers/llm.py`, `app/pipeline/gates.py`, `prompts/*`, `tests/test_prompt.py`
**Do not touch** `tests/test_gates.py`, `tests/fixtures/` — pre-written (§0.1)
**Depends on** SPEC §7, §8, §10
**Verify** `uv run pytest tests/test_gates.py tests/test_prompt.py -q`
**Gate** Every fixture classified correctly; each gate returns a named `GateFailure` rather
than raising; G2 enforces **exactly 5 scenes, 25–38 words each, 125–190 total**;
`RecordedLLMProvider` replays with no network
**Commit** `feat(pipeline): add script generation with validation gates G1-G4`

**The round most worth recording.** Hand the agent a failing test file: *make these pass,
do not edit the tests, show me the pytest output.* That converts "is it done" from an
opinion into an observable fact.

### Round 6 — TTS, audio-first timing, gate G5 · 25 min

**Goal** Narration synthesised, durations **measured**, timing derived from audio.
**Touches** `app/providers/tts.py`, `app/pipeline/orchestrator.py`, `tests/test_pipeline.py`
**Depends on** SPEC §9.2, §10
**Verify** `uv run pytest tests/test_pipeline.py -q`, then one real run
**Gate** Durations come from ffprobe, never word count; **G5a** (integrity) retries TTS with
backoff; **G5b** (total duration) uses the fallback script rather than retrying TTS; cache
hits on identical text
**Commit** `feat(providers): add edge-tts narration with measured durations`

The G5a/G5b split matters: a 95-second total is a script problem, and re-running TTS on
the same text produces the same 95 seconds. The word budget in round 5 is what prevents
this at source; G5b is the net, and the harness will likely show it never fires.

> **Checkpoint B.** Real, gated, concept-anchored scripts with matching narration audio.
> Everything downstream is deterministic.

---

## Block C — video and reliability

### Round 7 — Renderer, theme, mux, gates G6–G7 · 50 min

**Goal** A real, watchable MP4.
**Touches** `app/providers/visual.py`, `app/providers/theme.py`, `app/providers/scenes/*`,
`app/providers/ffmpeg.py`
**Depends on** SPEC §7, §9.3–9.5, §13
**Verify** `uv run python -m scripts.demo` then open the mp4
**Gate** ffprobe reports both streams; duration 45–90s; captions legible; crossfades present;
G7 rejects a deliberately corrupted file
**Commit** `feat(providers): add matplotlib renderer and ffmpeg mux pipeline`

**One static PNG per scene — five `savefig` calls per video, under a second total.**
All motion comes from ffmpeg at encode time: 0.4s crossfades between scenes plus a subtle
`zoompan` 1.00 → 1.04. No frame-by-frame animation anywhere.
*(Round 10 removed the `zoompan` — it trembled. See Part 3 cut 1 and `SPEC.md` §9.4.)*

That choice is the cost/quality answer in miniature: quality comes from theme, layout,
typography and captions — work done once that applies to every run — while motion comes
free from the encoder. It also keeps a full harness pass near ten minutes instead of
forty.

Largest round. If it overruns, cut `zoompan` first, then crossfades. Captions and the
theme are not the cut line.

### Round 8 — Retry, fallback, degraded, manifest · 30 min

**Goal** Failure paths proven, `manifest.json` emitted.
**Touches** `app/pipeline/retry.py`, `app/pipeline/cost.py`, `app/concepts/fallbacks/*`,
`tests/test_pipeline.py`
**Do not touch** `tests/test_cost.py` — pre-written (§0.1)
**Depends on** SPEC §9.1, §12, §14
**Verify** `uv run pytest tests/test_pipeline.py tests/test_cost.py -q`
**Gate** With an always-failing LLM the job still completes with `degraded=true`; retry
feedback reaches the prompt; `manifest.json` records every gate attempt; cost matches §14
**Commit** `feat(pipeline): add retry feedback, fallback scripts and run manifest`

> **Checkpoint C.** The system is submission-ready. Everything after this is evidence
> and documentation.

---

## Block D — evidence and deliverables

### Round 9 — Reliability harness · start early, ~12 min unattended

**Goal** The evidence.
**Touches** `scripts/harness.py`, `reports/`
**Depends on** SPEC §15
**Verify** `uv run python -m scripts.harness --runs 5`
**Gate** `reports/reliability.md` with the full table plus per-gate failure counts; zero
`failed` jobs
**Commit** `feat(harness): add reliability harness and generated report`

**Start the harness, then write round 10 while it runs.** Free-tier pacing makes it
sequential; three concepts × five runs lands near twelve minutes.

### Round 10 — Deliverables · 35 min

**Goal** Everything the brief asks for, in the shape it asks for.
**Touches** `README.md`, `ARCHITECTURE.md`, `submissions/`, `tests/test_contracts.py`
(added by approval: CLAUDE.md §8 names it as the T3 slow suite but no round claimed it)
**Commit** `docs: add readme, architecture note and submitted runs`

`README.md` — setup · run · API reference · **test instructions** · cost model · what you
optimised for · how flaky generation is avoided.

`ARCHITECTURE.md` — job lifecycle · persistence/artifact boundary · AI/video-generation
boundary · the build/fake/simplify/leave-out table · known risks (edge-tts is an
unofficial endpoint; free-tier data may be used for training).

Both written **after** the harness so the numbers are real.

Then `uv run python -m scripts.demo --out ./submissions` for the final committed runs,
and record the demo walkthrough.

---

## Part 2 — Submission checklist

- [ ] FastAPI codebase
- [ ] `README.md` with setup, run, API, **test** instructions
- [ ] `ARCHITECTURE.md` as a **separate file**, all three required boundaries
- [ ] Three best videos in `submissions/`, each with originating query, `script.json`,
      `manifest.json`; filenames embed the `job_id`
- [ ] Demo recording covering **all three** concepts
- [ ] GitHub read access: `careers@growtrics.ai`, `praveen.k@growtrics.ai`,
      `wayne.le@growtrics.ai`
- [ ] Zip excluding `.venv/`, `__pycache__/`, `artifacts/`
- [ ] Google Drive link, sharing verified, **file confirmed playable**
- [ ] Email to `careers@growtrics.ai`, CC `praveen.k@` and `wayne.le@`

---

## Part 3 — If time runs out

Cut in this order. Never cut upward past the line.

1. ~~`zoompan` subtle zoom → plain stills~~ — **taken in round 10**, not for time but
   because it visibly trembled. Encode got 2.2× faster as a side effect.
2. Crossfades → hard cuts
3. Harness runs 5 → 3
4. `GET /concepts` endpoint
5. Audio caching

**Never cut:** G3, G4, G7, fallback + `degraded`, `manifest.json`, the harness report.
Those five carry the entire reliability argument — the criterion the brief calls both
the most important and the most commonly failed.
