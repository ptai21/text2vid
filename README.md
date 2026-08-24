# text2vid

A learner asks a chemistry question in plain English. The service accepts it as an
asynchronous job, returns a job id immediately, and produces a narrated 45–90 second
educational video in the background — visuals and voice, not a slideshow and not an
audio file.

Three concepts are supported end to end: **the pH scale**, **why atoms form covalent
bonds**, and **ionic vs covalent bonding**.

- **Contracts:** [SPEC.md](SPEC.md) — API, schemas, gates, cost model
- **Design and boundaries:** [ARCHITECTURE.md](ARCHITECTURE.md)
- **Measured reliability:** [reports/reliability.md](reports/reliability.md)
- **Everything that went wrong on the way:** [reports/findings.md](reports/findings.md)

---

## Quick start

Requires **Python 3.11.8**, [`uv`](https://docs.astral.sh/uv/), and **ffmpeg + ffprobe**
on `PATH`.

```bash
uv sync
cp .env.example .env          # then paste a Gemini API key into GEMINI_API_KEY
uv run python -m scripts.smoke
```

`scripts.smoke` verifies the four external dependencies independently — Gemini,
edge-tts, matplotlib, ffmpeg — so a failure names which one, before a job ever runs.

```bash
uv run uvicorn app.main:app --reload
```

Interactive docs at **http://127.0.0.1:8000/docs**.

Everything runs through `uv run`. If a command only works after manually activating a
venv, something is wrong.

---

## Try it

```bash
# Submit. Returns 202 immediately with a job id.
curl -s -X POST localhost:8000/videos \
  -H 'content-type: application/json' \
  -d '{"query": "How does the pH scale work?"}'

# Poll. `stage` moves resolving -> scripting -> narrating -> rendering -> muxing -> publishing.
curl -s localhost:8000/videos/<job_id>

# Watch it.
curl -s localhost:8000/videos/<job_id>/artifact -o video.mp4

# Why it cost what it cost, and which gates ran.
curl -s localhost:8000/videos/<job_id>/manifest
```

An unsupported question is rejected at submit time, **before any spend**:

```bash
curl -s -X POST localhost:8000/videos \
  -H 'content-type: application/json' -d '{"query": "How do volcanoes work?"}'
# 400  {"error": {"code": "unsupported_concept", ...,
#                 "supported_concepts": ["ph_scale", "covalent_bonds", "ionic_vs_covalent"]}}
```

Or generate all three without the server:

```bash
uv run python -m scripts.demo --out ./submissions
```

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/videos` | Submit a query. `202` + job id, or `400` naming why it was refused. |
| `GET` | `/videos` | List jobs. Filters: `status`, `concept`, `limit`, `offset`. |
| `GET` | `/videos/{job_id}` | Full job: status, stage, attempts, degraded, failure, cost, timings. |
| `GET` | `/videos/{job_id}/artifact` | The MP4. `409` with current `status`/`stage` if not ready yet. |
| `GET` | `/videos/{job_id}/manifest` | The run record — gates, tokens, per-stage timings. |
| `GET` | `/concepts` | Supported concepts and their aliases. |
| `GET` | `/health` | Liveness, ffmpeg presence, queue depth. |

Two details worth calling out, because they are the difference between a status endpoint
and a *useful* status endpoint:

**A pending artifact is `409`, not `404`.** `404` tells a polling client the job does not
exist. `409` plus the current `status` and `stage` tells it to wait, and roughly how far
along it is. The brief says latency is not the concern — clearly exposing the waiting
state is.

**Every failure is named.** One error envelope everywhere, nine distinct `FailureCode`
values — `invalid_request`, `unsupported_concept` and `ambiguous_query` at the door, then
`script_unavailable`, `tts_failed`, `render_failed`, `mux_failed` and `artifact_invalid`
from the five pipeline stages, with `internal_error` as the catch-all that means *bug*.
Tracebacks are never returned; they are logged against the `job_id`.

One honest exception: a job that exceeds `JOB_TIMEOUT_S` currently reports
`internal_error` too. A timeout is a foreseen condition, not a bug, so it should carry its
own code — noted in [findings.md](reports/findings.md) rather than quietly conflated.

Full request/response shapes: [SPEC.md §5](SPEC.md).

---

## Tests

```bash
uv run pytest -q                    # 325 tests, ~5s, no network, no quota
uv run pytest -m slow               # contract tests against real edge-tts / ffmpeg / Gemini
uv run pytest tests/test_gates.py -q
```

The fast suite is deliberately network-free and quota-free. LLM behaviour is replayed
from recorded fixtures in `tests/fixtures/llm/` — **including deliberately broken ones**:
truncated JSON, four scenes instead of five, an unknown visual type, a missing concept
anchor. That is how the gates are proven to reject rather than merely asserted to.

Two commands that are not tests but earn their keep:

```bash
uv run python -m scripts.render_demo --concept ph_scale   # fixture -> TTS -> render -> mux, no Gemini quota
uv run python -m scripts.render_demo --corrupt            # proves G7 rejects a broken artifact
uv run python -m scripts.harness --runs 5                 # the reliability distribution
```

---

## Reliability — what was actually measured

Full report: [reports/reliability.md](reports/reliability.md). Fifteen runs, three
concepts, real Gemini and real edge-tts:

| | |
|---|---|
| Completed | **15 / 15** — 0 failed, 0 degraded |
| First attempt | 13 / 15. The other 2 were rejected by a gate and recovered on retry |
| Duration | 53.9 – 78.5s, every run inside the 45–90s window |
| Cost | $0.0151 – $0.0231 per video (production estimate) |
| Acceptance criteria | 5 / 5 PASS ([SPEC.md §15](SPEC.md)) |

### How flaky generation is avoided

This is the part the brief says most submissions get wrong, so here is the actual
mechanism rather than a claim.

**1. Only one stage is non-deterministic, and it is the cheapest one to redo.**
The LLM produces a structured script — never pixels, never code, never a rendering
instruction. Visuals come from a closed `VisualType` enum rendered by committed
matplotlib code. A generative video API would have meant 8–10 independently
non-deterministic clips per 75-second video, stitched and hoped to stay consistent.
Programmatic rendering **removes** that source of variance rather than managing it.

**2. The learner's raw query never enters the prompt.**
Not as instruction, not as context. The resolver maps the query to a `ConceptKey` by
rule, and the prompt is built from that concept's contract. This is a structural
guarantee, not a filter: prompt injection has nothing to inject into, and rephrasing
"how does pH work?" cannot move the content. The query still reaches the learner — it is
printed on the title card, so the video is visibly an answer to *their* question.

**3. LLM output is treated as untrusted input.**
Seven gates run in order and stop at the first failure. Each **returns** a named
`GateFailure` rather than raising, so the reason is data the retry can use:

| Gate | Checks |
|---|---|
| G1 | Parses as the declared schema at all |
| G2 | Exactly 5 scenes, 25–38 words each, 125–190 total, no markdown, no bullets |
| G3 | Every `visual` is in the enum **and** carries the params its renderer requires |
| G4 | The concept's required anchors appear, and required visuals are present |
| G5 | Measured narration exists per scene (G5a) and totals 45–90s (G5b) |
| G6 | Every frame written, readable, correct dimensions |
| G7 | Final MP4 has both streams, plausible size, duration matching the narration |

**4. A retry is informed, or it is just a second lottery ticket.**
On failure the gate name and its detail are fed back into a third prompt layer. Attempt 2
is told *"G4: the narration never mentions the logarithm"* — not asked to try again.
Bounded at 3 attempts.

**5. The scripting stage cannot fail.**
After 3 attempts a pre-committed fallback script is loaded — one per concept, each
re-validated through G1–G4 on load, each measured through real TTS (59.6s / 53.3s /
55.8s). A degraded run always sets `degraded: true` and says so in the manifest. A
fallback served *without* the flag would be canned output pretending to be generation;
the flag is what makes it a declared degradation path instead.

**6. Timing is measured, never estimated.**
TTS runs first, ffprobe measures each clip, and those durations drive the visual timing.
Sync is a consequence rather than a calibration task. The crossfade arithmetic is exact:
N stills joined by N−1 transitions of length T run for `sum − (N−1)·T`, so each still
after the first is held T longer and the video lands back on the narration length. Across
three verification runs, expected 63.3 / 60.4 / 68.5s → actual 63.3 / 60.5 / 68.5s.

**7. A job cannot be marked `completed` on a broken artifact.**
G7 probes the finished file. `--corrupt` proves it: truncate the encode, drop the audio
track, or drift the duration, and G7 rejects all three while passing the intact file.

### About the gates that never fired

Across 15 runs, **G2 rejected 2 scripts and G1, G3 and G4 rejected none.** R8 says a
guardrail has to earn its place, so the zeroes are reported rather than quietly enjoyed —
and the case for keeping them is history, not the counter:

- **G3 caught a bug no unit test could.** The Gemini `response_schema` declared `params`
  as a bare object, and constrained generation emits only *declared* properties — so
  `params` came back empty on all three live calls. Every fixture already had `params`
  populated, so the fast suite was green throughout. G3 is the only reason that reached a
  log instead of a learner.
- **G4 caught the model writing `"seven"` where the anchor expected `"7"`.**
- **G2 is the one still firing.** Twice in the harness, both on `ionic_vs_covalent` — a
  scene outside the 25–38 word range — and both retries succeeded. Then a third case from
  outside the harness entirely: the first live call in `tests/test_contracts.py` came back
  at **115 words against the 125 minimum**. Without the retry loop those are three degraded
  jobs instead of three clean ones, which is the concrete evidence that the retry is
  load-bearing rather than decorative.

That last one is worth its own sentence. Fifteen harness runs never produced a
`total_words` rejection; the first contract-test run did. It is also the opposite of the
failure that was anticipated — **the model undershoots the word budget, it does not run
long.**

A cheap check that prevents an expensive failure is justified even in the runs where it
sits idle. What would *not* be justified is inferring that from a column of zeroes, so it
is argued here instead.

### One honest asymmetry

`ionic_vs_covalent` is measurably the hardest of the three: 3/5 on first attempt against
5/5 for the other two, the longest videos (up to 78.5s), and the highest cost. It carries
two visual types and a comparison table, which makes it the most constrained prompt. Both
gate rejections in the entire run landed there. Averaging that away would hide the most
useful signal in the dataset.

---

## Cost

**≈ $0.018 per video** in production terms; **$0** in development, since edge-tts is free
and the Gemini free tier bills nothing. Actual metered LLM spend across all 15 harness
runs was **$0.0317**.

Cost is tracked per job from real token counts — not estimated — and **failed attempts
are billed**. A degraded job is not a free job: a model that answers three times with
garbage still costs three calls. Only a call that *raises* costs nothing.

| Item | Production | Share |
|---|---|---|
| TTS (Azure Neural, $16/1M chars) | $0.0160 | 65–75% |
| LLM (`gemini-3.5-flash-lite`, $0.30/$2.50 per 1M) | $0.0038–0.0071 | 18–29% |
| Render + encode, storage, egress | $0.0014 | remainder |

Two findings worth more than the total:

**TTS dominates, not the LLM.** So the highest-leverage optimisation is caching narration
audio by `hash(text + voice)` — implemented — and if cost has to fall further the move is
self-hosted TTS (Piper, Kokoro), not a cheaper model.

**Thinking tokens were zero on all 21 live calls.** `gemini-3.5-flash-lite` is a thinking
model and thinking tokens bill at the *output* rate while never appearing in
`response.text`, so a cost model counting only `candidates_token_count` under-reports.
This one counts them explicitly. Under a `response_schema` the model turns out not to
think at all — but the model is priced for the case where it does, because on a
reasoning-heavier model that term is what flips the TTS/LLM ranking above.

**Versus generative video:** $0.10–0.40 per second × 75s = **$7.50–30 per video**, roughly
400–1600× more. Price is the weaker half of the argument; see mechanism #1.

---

## What this optimised for

Reliability first, then cost, then visual polish — in that order, deliberately.

Consistently good beats occasionally excellent for something a learner submits to. So the
budget went to making the non-deterministic surface small and validated, rather than to a
richer visual library. The visible cost of that choice: **eight visual types, and a new
one needs code rather than a prompt change.** That is a real limit, and it is the price of
G3 being able to guarantee that anything the model asks for can actually be drawn.

A fourth concept is one registry entry — `ConceptContract` data, no pipeline change, no
prompt change. `GET /concepts` shows the extension point from the API alone. The scope
lock is three concepts on purpose; the registry is how extensibility is demonstrated
without building a fourth.

---

## Known limits and risks

| | |
|---|---|
| **edge-tts is an unofficial endpoint.** | It can be rate-limited or broken without notice. Mitigated by an explicit timeout, 3× backoff, a sha256 audio cache, and the `TTSProvider` seam — swapping to Azure Neural TTS is one class. Not something to build a business on unmitigated. |
| **Gemini free tier.** | 5–15 RPM, ~1,000–1,500 requests/day, and **free-tier data may be used to improve Google's products.** Fine for a prototype; not acceptable for real learner data. A paid tier changes the terms, not the code. |
| **Jobs are in memory.** | A restart loses them. `JobRepository` is a Protocol and `InMemoryJobRepository` is one implementation of it; the signatures are already async so a real database never touches a call site. |
| **Artifacts are local files.** | Single node. Same story — `ArtifactStore` is a Protocol. |
| **Encoding dominates wall time.** | ~35s of a ~46s job. A tradeoff, not a defect: the brief states latency is not a concern. `scripts/harness.py --scripts-only` exists so reliability can be sampled at high N without paying it. |
| **Paraphrases outside the alias sets are rejected.** | Deliberate. A clear "not supported, here are the three that are" beats an LLM router guessing wrong, adding cost and adding a second source of non-determinism at the front door. |

---

## Repository map

```
app/
  api/          HTTP only — validate, call pipeline, map to HTTP
  domain/       pure Python, no I/O — Job, state machine, Script
  concepts/     data — registry, aliases, fallback scripts
  pipeline/     runner, orchestrator, gates, retry, cost
  providers/    the only place external dependencies are touched
  storage/      JobRepository + ArtifactStore
prompts/        three layers: invariant / concept contract / gate feedback
scripts/        smoke, demo, render_demo, harness
tests/          325 fast + slow contract tests
submissions/    the three runs submitted, with script.json and manifest.json
```

`domain/` imports nothing from the layers above it. Gemini, edge-tts and ffmpeg are
reachable **only** through `providers/` — no SDK call and no `subprocess` anywhere else in
the tree. See [ARCHITECTURE.md](ARCHITECTURE.md) for why the boundaries fall there.
