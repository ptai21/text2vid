# CLAUDE.md

Operating rules for any AI coding agent working in this repository.
Read fully before the first edit.

**Document authority.** `CLAUDE.md` = what is allowed. `SPEC.md` = what correct
behaviour is. `PLAN.md` = what to do next. On conflict, that split decides.

---

## 1. Context

Backend take-home for Growtrics. A learner submits a natural-language chemistry
question; the backend accepts it as an **asynchronous video-generation job**, produces a
short narrated educational video (45–90s, visuals + audio), and exposes job status plus
the finished artifact over REST.

**The brief states: "Completion is not the end goal."** It says so twice — the only
repeated sentence in the document. Judgement beats surface area. Every rule below
follows from it.

### Graded criteria, in the evaluators' stated order

| # | Criterion | Consequence for the code |
|---|-----------|--------------------------|
| 1 | **Reliability under non-determinism** | LLM output is **untrusted input**. Validate before it reaches the learner. Repeated runs must be consistently good. The brief calls this the weakest area of most submissions. |
| 2 | **Product judgement & architecture** | Clean API, explicit job lifecycle, clean boundaries. Documented tradeoffs. |
| 3 | **AI-agent workflow** | Visible planning, verifiable steps, inspected output. |
| 4 | **Quality** | API clarity, job-state handling, error handling, observability. |

Cost-efficiency and visual quality are **both** explicit success metrics. Cheap and
controllable beats expensive and impressive-but-flaky.

---

## 2. Scope lock

**In scope — exactly three learner queries, end to end:**

1. How does the pH scale work?
2. Why do atoms form covalent bonds?
3. What is the difference between ionic and covalent bonding?

**Out of scope — do not build, do not propose building:**

Frontend of any kind · auth / users / rate limiting · database, ORM, migrations ·
message broker (Celery, Redis, RQ) · Docker, CI, deployment · generative video APIs ·
cache layer · webhook callbacks · job cancel/delete · HTTP range requests ·
multi-language · a fourth concept.

Extensibility to other STEM topics is demonstrated **through the concept registry**,
not by implementing extra topics.

---

## 3. Rules taken from the brief

- **R1** Backend only. FastAPI. No frontend.
- **R2** Submitting returns immediately with a job id; generation runs in background.
  Latency is explicitly not a concern — *clearly exposing the waiting state* is.
- **R3** Clients can list jobs and read any single job's status.
- **R4** A completed job exposes a playable video artifact.
- **R5** Narration **and** visuals. Not a silent slideshow, not an audio file.
- **R6** The explanation must be coherent, useful, and visibly tied to the query.
- **R7** Failures are named and explicit. Never silent, never half-completed.
- **R8** Guardrails must **earn their place**. Blanket retries on every layer are a
  negative signal. Each guardrail has a written reason in `SPEC.md`.
- **R9** It must be obvious where a real AI/video provider plugs in.
- **R10** Per-artifact cost is tracked and reportable.

---

## 4. Settled decisions — do not revisit

If the agent believes one is wrong it must **stop and raise it**, not silently diverge.

| Decision | Rationale |
|----------|-----------|
| Programmatic rendering (matplotlib + ffmpeg), not generative video | ~400–1600× cheaper, and **removes a non-determinism source rather than managing one**. Generative APIs cap at 8–10s clips, so a 75s video means 8–10 independently non-deterministic generations stitched together. |
| LLM produces a structured script only — never pixels, never code | Confines non-determinism to one validatable layer. |
| Audio-first timing | TTS duration is measured, then drives visual timing. Sync becomes a consequence, not a calibration task. |
| Rule-based concept resolution, no LLM router | Avoids adding non-determinism and cost at the front door. Clear rejection beats a wrong guess. |
| The learner's raw query never enters the prompt as instruction | Blocks prompt injection and stops phrasing variance from moving the content. The query is stored in job metadata and shown on the title card (satisfies R6). |
| In-memory persistence behind `JobRepository`; local files behind `ArtifactStore` | Permitted by the brief when the boundary is clean. Swapping must be a one-class change. |
| Pre-committed fallback script per concept | Makes the script stage unable to fail. Always flagged `degraded: true`. |
| `gemini-3.5-flash-lite` | Cheapest current model; constrained JSON generation does not need more. Thinking tokens bill as output tokens, so the cost model must count them (§14). **Gemini 2.0 Flash was shut down 2026-06-01 — never use that model string.** |

---

## 5. Hard constraints on agent behaviour

**Stop and ask** before any of these:

- Adding, removing, or upgrading a dependency.
- Changing the API contract in `SPEC.md` §5 (paths, methods, shapes, status codes).
- Changing the script schema (`SPEC.md` §7) or the `VisualType` enum.
- Changing the job state machine (`SPEC.md` §4).
- Editing any file outside the current round's `Touches` list in `PLAN.md`.
- Introducing a layer, package, or abstraction not described in `SPEC.md`.
- Weakening, skipping, or deleting a validation gate.
- **Editing anything under `tests/`** when the round says tests are pre-written.

Asking costs one message. Silently diverging costs a rebuild.

---

## 6. Environment

| Item | Value |
|------|-------|
| OS / shell | Windows, **Git Bash** |
| Python | 3.11.8 |
| Package manager | **`uv` only** — never `pip`, never `python -m venv` |
| Config | `pydantic-settings` + `.env` — never `os.environ` directly |
| LLM | `google-genai`, model `gemini-3.5-flash-lite`, structured output |
| TTS | `edge-tts` (free, no API key) |
| Rendering | `matplotlib` (**Agg backend**) + `ffmpeg` / `ffprobe` |
| Web | FastAPI + Uvicorn |
| Logging | `structlog`, JSON to stdout |

Every command runs through `uv run`. If something works only after manually activating
a venv, it is wrong.

---

## 7. Commands

```bash
uv sync                                   # install / refresh
uv run uvicorn app.main:app --reload      # API, docs at /docs
uv run pytest -q                          # fast suite (T1+T2), must stay under ~3s
uv run pytest -m slow                     # contract tests that touch real I/O
uv run pytest tests/test_gates.py -q      # single file
uv run python -m scripts.harness --runs 5 # reliability harness
uv run python -m scripts.smoke            # environment smoke test
uv add <pkg>                              # ONLY after explicit approval (§5)
```

---

## 8. Repository layout

```
text2vid/
├── CLAUDE.md               agent rules (this file)
├── SPEC.md                 contracts
├── PLAN.md                 build order + setup
├── README.md               deliverable — written last
├── ARCHITECTURE.md         deliverable — written last
├── pyproject.toml
├── .env.example
│
├── app/
│   ├── main.py             app assembly + wiring
│   ├── config.py           pydantic-settings
│   ├── logging.py          structlog setup
│   │
│   ├── api/                HTTP ONLY
│   │   ├── routes.py
│   │   ├── schemas.py      request/response models
│   │   └── errors.py       exception handlers, error envelope
│   │
│   ├── domain/             PURE PYTHON, NO I/O
│   │   ├── job.py          Job, JobStatus, JobStage, FailureCode
│   │   ├── state.py        transition table + guard
│   │   └── script.py       Script, Scene, VisualType, VisualSpec
│   │
│   ├── concepts/           DATA, not logic
│   │   ├── registry.py     ConceptKey → ConceptContract
│   │   ├── aliases.py      query → ConceptKey matching
│   │   └── fallbacks/      ph_scale.json, covalent_bonds.json, ionic_vs_covalent.json
│   │
│   ├── pipeline/
│   │   ├── runner.py       async worker + semaphore
│   │   ├── orchestrator.py stage sequencing
│   │   ├── gates.py        G1–G7
│   │   ├── retry.py        retry + fallback policy
│   │   └── cost.py         cost accounting
│   │
│   ├── providers/          ONLY place external deps are touched
│   │   ├── llm.py          LLMProvider, GeminiProvider, RecordedLLMProvider
│   │   ├── tts.py          TTSProvider, EdgeTTSProvider
│   │   ├── visual.py       VisualProvider, MatplotlibProvider
│   │   ├── theme.py        rcParams, palette, layout grid
│   │   ├── scenes/         one module per VisualType
│   │   └── ffmpeg.py       mux, probe, loudnorm
│   │
│   └── storage/
│       ├── repository.py   JobRepository + InMemoryJobRepository
│       └── artifacts.py    ArtifactStore + LocalArtifactStore
│
├── prompts/
│   ├── system.md           layer 1 — invariant
│   ├── concept.md.j2       layer 2 — concept contract
│   └── retry.md.j2         layer 3 — gate feedback
│
├── scripts/
│   ├── smoke.py            verify env: gemini, edge-tts, matplotlib, ffmpeg
│   └── harness.py          3 concepts × N runs → reports/reliability.md
│
├── tests/
│   ├── test_state.py       T1
│   ├── test_resolver.py    T1
│   ├── test_gates.py       T1 — the core file
│   ├── test_prompt.py      T1
│   ├── test_cost.py        T1
│   ├── test_pipeline.py    T2 — fake providers
│   ├── test_api.py         T2
│   ├── test_contracts.py   T3 — @pytest.mark.slow, real I/O
│   └── fixtures/llm/       valid + deliberately broken LLM outputs
│
├── artifacts/              gitignored except the three submitted runs
│   └── {job_id}/video.mp4, script.json, manifest.json
│
├── submissions/            the three best runs, committed
└── reports/reliability.md  generated by the harness
```

Enforced import rules:

- `api/` holds **no business logic** — validate, call pipeline, map to HTTP.
- `domain/` imports nothing from `api/`, `pipeline/`, `providers/`, `storage/`.
- `pipeline/` never imports FastAPI or touches HTTP concepts.
- Gemini, edge-tts, and ffmpeg are reachable **only** through `providers/`. No direct
  SDK call or `subprocess` anywhere else.
- `concepts/` is data. If logic accumulates there, it belongs in `pipeline/`.

Violating one of these is a design change → §5 → ask first.

---

## 9. Commit rules

**Conventional Commits**, one commit per completed round, minimum.

```
<type>(<scope>): <imperative summary>

<what changed, 1-3 lines>

Gate: <verify command>
Result: <observed output, e.g. "18 passed in 0.9s">
Round: <N of PLAN.md>
```

Types: `feat` `fix` `test` `refactor` `docs` `chore`.
Scopes: `api` `domain` `pipeline` `providers` `storage` `concepts` `prompts` `harness`.

Example:

```
feat(pipeline): add script validation gates G1-G4

Schema, structural, renderer-contract and concept-anchor gates.
Each returns a named GateFailure rather than raising.

Gate: uv run pytest tests/test_gates.py -q
Result: 22 passed in 0.7s
Round: 4 of PLAN.md
```

Rules:

- **Never commit on a failing gate.** The `Result:` line records something that was
  actually observed. Writing it without running the command is a §11 violation.
- Docs (`CLAUDE.md`, `SPEC.md`, `PLAN.md`) are committed **before** round 1. Their
  timestamps are evidence of planning-before-implementation and must not be rewritten.
- One round = one logical commit. Do not squash rounds together; the history is part of
  the submission.
- Never amend or force-push a commit that has already passed its gate.
- `artifacts/` is gitignored. The three submitted runs live in `submissions/` and are
  committed with their `script.json` and `manifest.json` alongside.

---

## 10. Definition of Done

A round is complete when **all** hold:

1. The round's `Verify` command in `PLAN.md` runs clean.
2. The round's `Gate` was **observed**, not assumed.
3. No file outside `Touches` was modified.
4. No dependency added without approval.
5. Committed in the format above.

"It should work now" is not done. Run the command and read the output.

---

## 11. Forbidden — these fail the challenge

- Editing a test assertion to make it pass instead of fixing the code.
- `except Exception: pass`, or any handler that swallows an error without recording a
  named `FailureCode`.
- Hardcoding, memoising, or caching the three concept scripts so the pipeline *appears*
  to call the LLM while serving canned output. Fallback scripts are a declared,
  flagged degradation path and always set `degraded: true` — that is different.
- Marking a job `completed` when the artifact is missing, zero-byte, has no audio
  stream, or is shorter than the minimum duration.
- Tests that mock the thing under test.
- Skipping a gate to get an end-to-end run working "for now".
- Claiming a gate passed without running it.
- Building anything from the out-of-scope list in §2.

---

## 12. When stuck

Do not guess, do not expand scope to route around a blocker. Report:

1. What was attempted, with exact command output.
2. Suspected cause.
3. Two options with tradeoffs.
4. A recommendation.

Then wait. If a round fails its gate twice, stop — the rollback rule in `PLAN.md`
applies. Do not stack fixes on a broken round.
