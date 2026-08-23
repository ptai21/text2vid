# SPEC.md

System contract for the AI chemistry video request service.
This file defines **what correct behaviour is**. Rules and permissions live in
`CLAUDE.md`; build order lives in `PLAN.md`.

---

## 1. Scope and non-goals

**Goal.** Accept a natural-language chemistry question, return a job id immediately,
generate a 45–90s narrated educational video in the background, expose status and the
finished artifact over REST.

**Supported concepts.** Exactly three (§6). Anything else is rejected with a named
failure, not guessed at.

**Non-goals** are listed in `CLAUDE.md` §2 and are deliberate, not unfinished.

---

## 2. Architecture flow

```
  client
    │  POST /videos {"query": "..."}
    ▼
┌─────────────────────────────────────────────────────────┐
│ api/          validate → resolve → create job → 202     │  synchronous
└─────────────────────────────────────────────────────────┘
    │ enqueue(job_id)
    ▼
┌─────────────────────────────────────────────────────────┐
│ pipeline/runner   asyncio worker, semaphore(2)          │  background
│   └─ orchestrator                                       │
│        script ──▶ narrate ──▶ render ──▶ mux ──▶ publish│
│        G1-G4      G5          G6         G7             │
└─────────────────────────────────────────────────────────┘
    │                    │                    │
    ▼                    ▼                    ▼
 providers/          storage/            concepts/
 LLM, TTS,           JobRepository       registry, aliases,
 Visual, ffmpeg      ArtifactStore       fallback scripts
```

Dependency direction is strictly downward: `api → pipeline → providers/storage`, and
everything may import `domain`. `domain` imports nothing.

---

## 3. Domain model

```python
ConceptKey   = Literal["ph_scale", "covalent_bonds", "ionic_vs_covalent"]
JobStatus    = Literal["queued", "running", "completed", "failed"]
JobStage     = Literal["resolving","scripting","narrating","rendering","muxing","publishing"]

class Job:
    job_id: str              # uuid4
    query: str               # learner's raw text, verbatim
    concept: ConceptKey | None
    status: JobStatus
    stage: JobStage | None
    degraded: bool           # fallback script was used
    attempts: int            # LLM attempts consumed
    failure: Failure | None
    artifact: ArtifactRef | None
    cost: CostBreakdown
    timings: dict[str, float]    # stage -> seconds
    created_at / updated_at: datetime

class Failure:
    code: FailureCode
    stage: JobStage
    message: str             # human-readable, safe to expose
    detail: str | None       # internal, logged not returned
```

**Why `status` and `stage` are separate.** Clients only need "is it done"; operators
need "where did it die". Collapsing both into one enum would force every client to know
the internals of the pipeline. `failure.code` supplies the named failure states R7
requires.

### FailureCode

| Code | Stage | Meaning |
|---|---|---|
| `invalid_request` | resolving | Empty, over-length, or malformed query |
| `unsupported_concept` | resolving | Valid question, outside the three concepts |
| `ambiguous_query` | resolving | Matches two or more concepts |
| `script_unavailable` | scripting | LLM failed **and** fallback missing (should be unreachable) |
| `tts_failed` | narrating | TTS exhausted retries |
| `render_failed` | rendering | Renderer raised or produced no frame |
| `mux_failed` | muxing | ffmpeg non-zero exit |
| `artifact_invalid` | muxing | G7 rejected the produced file |
| `internal_error` | any | Unhandled — always logged with traceback |

---

## 4. Job state machine

```
queued ──▶ running ──▶ completed        (terminal)
   │          │
   └──────────┴──────▶ failed           (terminal)
```

Stage advances only forward:
`resolving → scripting → narrating → rendering → muxing → publishing`

Rules:

1. `completed` requires a non-null `artifact` **and** a passed G7.
2. `failed` requires a non-null `failure`.
3. Terminal states never transition again. Illegal transitions raise
   `InvalidTransition` and are logged, never silently ignored.
4. A job may not stay in `running` without a stage.
5. `degraded=true` is compatible with `completed` — it is a quality flag, not a failure.

---

## 5. API contract

Base: `/`. All responses JSON except the artifact stream.

### `POST /videos`

```json
{"query": "How does the pH scale work?"}
```

`202 Accepted`

```json
{
  "job_id": "b3f1...",
  "status": "queued",
  "stage": null,
  "concept": "ph_scale",
  "query": "How does the pH scale work?",
  "created_at": "2026-08-23T10:00:00Z"
}
```

`400` with error envelope for `invalid_request`, `unsupported_concept`,
`ambiguous_query`. Rejection happens **before** any spend.

### `GET /videos`

Query params: `status`, `concept`, `limit` (default 20, max 100), `offset`.

```json
{"total": 12, "limit": 20, "offset": 0, "items": [ /* job summaries */ ]}
```

Summary = `job_id, query, concept, status, stage, degraded, created_at, duration_s`.

### `GET /videos/{job_id}`

`200` full job. `404` if unknown.

```json
{
  "job_id": "b3f1...",
  "query": "How does the pH scale work?",
  "concept": "ph_scale",
  "status": "completed",
  "stage": "publishing",
  "degraded": false,
  "attempts": 1,
  "failure": null,
  "artifact": {
    "url": "/videos/b3f1.../artifact",
    "duration_s": 68.4,
    "size_bytes": 7930112,
    "scenes": 5
  },
  "cost": {"llm_usd": 0.00055, "tts_usd": 0.0, "total_usd": 0.00055,
           "production_estimate_usd": 0.0181},
  "timings": {"scripting": 2.1, "narrating": 8.7, "rendering": 11.2, "muxing": 4.4},
  "created_at": "...", "updated_at": "..."
}
```

### `GET /videos/{job_id}/artifact`

`200 video/mp4` with `Content-Length` and
`Content-Disposition: inline; filename="{job_id}.mp4"`.
`404` unknown job. `409` job exists but not `completed` — envelope carries current
`status` and `stage` so a polling client knows to wait. Range requests are out of scope.

### `GET /videos/{job_id}/manifest`

`200` the run's `manifest.json` (§12). Observability surface.

### `GET /concepts`

Lists supported concepts with canonical question and aliases. Makes the extension point
visible to a reader of the API alone.

### `GET /health`

`200 {"status": "ok", "ffmpeg": true, "queue_depth": 0}`

### Error envelope

Every non-2xx uses one shape:

```json
{
  "error": {
    "code": "unsupported_concept",
    "message": "This service currently covers three chemistry concepts.",
    "supported_concepts": ["ph_scale", "covalent_bonds", "ionic_vs_covalent"]
  }
}
```

Tracebacks are never returned. `internal_error` returns a generic message and logs the
detail with the `job_id`.

---

## 6. Concept registry

Each entry is **data**. Adding a fourth STEM topic = adding one entry (plus any new
visual type). No pipeline change, no prompt change.

```python
class ConceptContract:
    key: ConceptKey
    canonical_question: str
    aliases: list[str]              # normalised, for the resolver
    narrative_shape: Literal["linear", "causal", "comparative"]
    beats: list[Beat]               # required content, ordered
    anchors: list[Anchor]           # G4 checks
    allowed_visuals: list[VisualType]
    forbidden_topics: list[str]     # drift guards
    fallback_path: Path
```

### 6.1 `ph_scale` — "How does the pH scale work?"

Shape: **linear**.

| Beat | Content |
|---|---|
| B1 | pH measures H⁺ (hydronium) concentration in solution |
| B2 | Scale runs 0–14; 7 is neutral |
| B3 | Below 7 acidic, above 7 basic/alkaline |
| B4 | **Logarithmic** — each step is a 10× change in H⁺ |
| B5 | Real anchors: lemon ~2, water 7, bleach ~13 |

Common LLM failure: dropping B4. It is the only beat that actually answers *how it
works*; without it the video is "small = sour, big = slippery" — true but empty.

Anchors (all required): `H+` or `hydrogen ion` · `7` **and** `neutral` · `acidic`
**and** (`basic` or `alkaline`) · `logarithmic` or `10 times`/`ten-fold`/`tenfold`.

Forbidden drift: titration, buffers, pKa.

Visuals: `title_card`, `ph_scale_bar`, `log_steps`, `summary_card`.

### 6.2 `covalent_bonds` — "Why do atoms form covalent bonds?"

Shape: **causal**. The question word is *Why*, not *What*. This is the hardest concept.

| Beat | Content |
|---|---|
| B1 | Atoms **share** electrons (not give/take) |
| B2 | Motive: a stable outer shell (octet; duet for H) |
| B3 | **Energy rationale**: the bonded state is lower in energy than two separate atoms |
| B4 | Force balance: nuclei–shared-pair attraction vs nucleus–nucleus repulsion |
| B5 | Example: H₂, and H₂O or CH₄ |

**The single most important failure mode in this project:** the model describes what a
covalent bond *is* instead of *why it forms*. The JSON is flawless, the prose is fluent,
and the answer is to a different question. Schema validation can never catch this. G4
catches it only because B3 is a hard anchor.

Anchors (all required): `share`/`shared`/`sharing` · `outer shell`/`valence`/`octet` ·
**`stable`/`stability`/`lower energy`** ← the word "why" lives here.

Forbidden drift: ionic bonding as the main subject, hybridisation, molecular orbitals.

Visuals: `title_card`, `atom_pair`, `energy_curve`, `summary_card`.

### 6.3 `ionic_vs_covalent` — "What is the difference between ionic and covalent bonding?"

Shape: **comparative**. Structure must be parallel — one axis, two sides.

| Beat | Axis |
|---|---|
| B1 | Mechanism: ionic **transfers** ↔ covalent **shares** |
| B2 | Participants: metal + non-metal (large electronegativity gap) ↔ non-metal + non-metal |
| B3 | Product: ions in a lattice ↔ discrete molecules |
| B4 | Properties: high melting point, conducts when molten/dissolved ↔ low melting point, usually does not |
| B5 | Paired examples: NaCl ↔ H₂O or CH₄ |

Common failure: explaining each type in turn and stopping — two mini-lectures glued
together, not a comparison.

Anchors: `ionic` **and** `covalent` present in ≥2 distinct scenes · `transfer` **and**
`share` · `NaCl` or `sodium chloride` · at least one contrasting property
(`melting point` or `conduct`).

**Structural anchor unique to this concept:** at least one scene must use
`side_by_side_comparison`. The comparative shape is enforced at the visual layer, not
only in prose.

Visuals: `title_card`, `electron_transfer`, `side_by_side_comparison`, `summary_card`.

### 6.4 Resolver

Normalise (lowercase, strip punctuation, collapse whitespace), then match against
alias sets. Deterministic, no LLM, no network, no cost.

- 0 matches → `unsupported_concept`, envelope lists the three supported concepts.
- ≥2 matches → `ambiguous_query`.
- Query length outside 3–500 chars → `invalid_request`.

The raw query is stored on the job and rendered on the title card. It is **never**
interpolated into the prompt as instruction — see `CLAUDE.md` §4.

---

## 7. Script schema

The LLM's only output. Generated under `responseSchema`.

```json
{
  "concept": "ph_scale",
  "scenes": [
    {
      "scene_id": "s1",
      "heading": "What pH measures",
      "narration": "Every water-based solution contains hydrogen ions...",
      "visual": {"type": "ph_scale_bar", "params": {"markers": [2.0, 7.0, 13.0]}}
    }
  ]
}
```

Constraints:

| Field | Rule |
|---|---|
| `scenes` | 4–6 items |
| `scene_id` | `s1..s6`, unique, ordered |
| `heading` | 1–6 words, sentence case |
| `narration` | 25–60 words; plain prose; no markdown, no bullet chars, no bare formulas that cannot be read aloud (`H₂O` → "water") |
| `visual.type` | must be in the concept's `allowed_visuals` |
| `visual.params` | must satisfy that type's param schema |

The model does **not** set scene duration. Duration is measured from the synthesised
audio (§9).

### VisualType enum (closed)

| Type | Params | Animated |
|---|---|---|
| `title_card` | — (query + concept title injected by renderer) | no |
| `ph_scale_bar` | `markers: float[0..14]`, ≤4 | **yes** — marker slides |
| `log_steps` | `from_ph: int`, `to_ph: int` | no |
| `atom_pair` | `left: str`, `right: str`, `shared_pairs: 1..3` | no |
| `energy_curve` | `min_distance: float`, `label: str` | **yes** — curve draws to minimum |
| `electron_transfer` | `donor: str`, `acceptor: str` | no |
| `side_by_side_comparison` | `left_title`, `right_title`, `rows: [[str,str]]` 2–4 | no |
| `summary_card` | `points: str[]` 2–4, each ≤10 words | no |

Two animated types, deliberately. Both animate the thing that *is* the explanation — the
pH marker and the energy minimum — rather than decorating. Every other type is a static
frame. See §16.

---

## 8. Prompt construction

Three layers. Layers 1 and 2 are files under `prompts/`, not f-strings.

**Layer 1 — invariant** (`prompts/system.md`): role, learner level, scene count,
narration length, style prohibitions, "choose `visual.type` only from the provided
list", JSON-only output.

**Layer 2 — concept contract** (`prompts/concept.md.j2`): rendered from the registry —
canonical question, ordered beats, `narrative_shape`, `allowed_visuals` with param
schemas, `forbidden_topics`.

**Layer 3 — retry feedback** (`prompts/retry.md.j2`): appended on attempts 2 and 3 only.
Names the gate that failed, the specific beat or anchor missing, and what is expected.

A retry without feedback is a second lottery ticket. A retry with feedback is
engineering. This is the justification R8 demands for the retry guardrail.

---

## 9. Pipeline stages

### 9.1 Scripting

Call `LLMProvider.generate_script(concept, feedback=None)`. Run G1→G4 in order; stop at
the first failure. On failure, retry with feedback, up to **2 retries (3 attempts
total)**. If all attempts fail, load the concept's fallback script and set
`degraded=true`.

**This stage cannot fail.** The fallback is pre-committed and has already passed every
gate, so a valid script always exists. `script_unavailable` exists only for a missing
fallback file — a deployment error, not a runtime one.

Consequence: *every job with a resolvable concept reaches `completed` unless TTS or
ffmpeg fails at the infrastructure level.* That is a testable claim, which is what §15
measures.

### 9.2 Narrating (audio-first)

For each scene: `TTSProvider.synthesize(narration) → mp3`, then **probe actual duration
with ffprobe**. Never estimate from word count. `scene.duration = probed + 0.4s` trailing
pad.

TTS failure is a network failure, so it gets a different policy from the LLM: 3 attempts
with exponential backoff (1s, 2s, 4s), per scene, 30s timeout each. On exhaustion the
job fails with `tts_failed` — a video without narration would violate R5, so there is no
sensible fallback here.

Audio is cached by `sha256(narration + voice_id)` — this protects the free-tier quota
during harness runs and is the highest-leverage cost lever in production (§14).

### 9.3 Rendering

`VisualProvider.render(scene, duration)` → PNG frames via matplotlib (Agg). Fully
deterministic: same input, same pixels. This stage contributes **zero** non-determinism,
which is the point of choosing programmatic rendering.

Static types emit one frame reused for the scene's duration. Animated types emit
`round(duration × 30)` frames.

Every frame carries the burned-in caption (§13).

### 9.4 Muxing

Per scene: frames + audio → segment. Then concat with 0.3s crossfades → single MP4.
H.264 `yuv420p`, AAC 128k, 1280×720, 30fps, `loudnorm` applied to the audio track.
`stderr` is always captured and, on failure, becomes `failure.detail`.

### 9.5 Publishing

Write `video.mp4`, `script.json`, `manifest.json` to the artifact store; record cost and
timings; transition to `completed`.

---

## 10. Validation gates

| Gate | Stage | Checks | On failure |
|---|---|---|---|
| **G1** Schema | scripting | Pydantic parse; required fields; types | retry w/ feedback |
| **G2** Structural | scripting | 4–6 scenes; ids unique/ordered; narration 25–60 words; no markdown; estimated total 45–90s | retry w/ feedback |
| **G3** Renderer contract | scripting | Every `visual.type` ∈ concept's `allowed_visuals`; every param present, correct type, in range | retry w/ feedback |
| **G4** Concept anchor | scripting | All required anchors present across the script; concept-specific structural rules; no `forbidden_topics` | retry w/ feedback |
| **G5** Audio | narrating | Each file exists, >0 bytes; per-scene duration 2–25s; total 45–90s | retry TTS, then fail |
| **G6** Frames | rendering | Expected frame count present; 1280×720; non-zero | fail `render_failed` |
| **G7** Artifact | muxing | ffprobe: **both** a video and an audio stream; duration within ±3s of expected; size > 200 KB | fail `artifact_invalid` |

**Why each earns its place** (R8):

- **G1** — structured output is a constraint, not a guarantee; malformed responses do
  occur. Cheapest possible check.
- **G2** — a 2-scene or 11-scene script breaks the product contract even when it parses.
- **G3** — the load-bearing technical gate. It guarantees **anything that passes can be
  rendered**. Without it the renderer can meet a `visual.type` it has never heard of.
- **G4** — the load-bearing product gate. It is the only check that catches perfectly
  valid JSON with off-target content, which is exactly the covalent-bond failure in §6.2.
  This gate is what makes R6 real.
- **G5** — audio drives timing; an empty or truncated file corrupts everything after it.
- **G6** — cheap, and turns a silent renderer bug into a named failure.
- **G7** — the last line of defence against the worst outcome: reporting `completed` for
  a zero-byte or silent file. This is the gate that protects trust in the demo.

Gates return a `GateFailure(gate, reason, detail)` — they do not raise.

---

## 11. Provider boundary

```python
class LLMProvider(Protocol):
    def generate_script(self, concept: ConceptContract,
                        feedback: GateFailure | None) -> RawScript: ...

class TTSProvider(Protocol):
    async def synthesize(self, text: str, out: Path) -> AudioResult: ...

class VisualProvider(Protocol):
    def render(self, scene: Scene, duration: float, out_dir: Path) -> list[Path]: ...
```

Implementations: `GeminiProvider` / `RecordedLLMProvider` · `EdgeTTSProvider` ·
`MatplotlibProvider`. Swapping to a generative video provider means one new
`VisualProvider`; swapping TTS to Azure means one new `TTSProvider`. That is the answer
to R9, and it is demonstrable rather than asserted.

`RecordedLLMProvider` replays captured responses — including deliberately broken ones.
It makes the test suite deterministic, network-free, and quota-free, and it is how the
gates are proven to work.

---

## 12. Persistence and artifacts

```python
class JobRepository(Protocol):
    def create(self, job: Job) -> None: ...
    def get(self, job_id: str) -> Job | None: ...
    def list(self, *, status=None, concept=None, limit=20, offset=0) -> tuple[list[Job], int]: ...
    def update(self, job: Job) -> None: ...

class ArtifactStore(Protocol):
    def put(self, job_id: str, name: str, src: Path) -> str: ...
    def open(self, job_id: str, name: str) -> BinaryIO: ...
    def exists(self, job_id: str, name: str) -> bool: ...
```

`InMemoryJobRepository` guards writes with an `asyncio.Lock`.
`LocalArtifactStore` writes to `artifacts/{job_id}/`.

### The artifact is a bundle, not a file

```
artifacts/{job_id}/
  video.mp4        deliverable for the learner
  script.json      exactly what the LLM produced (or the fallback)
  manifest.json    self-describing run record
```

`manifest.json`:

```json
{
  "job_id": "b3f1...",
  "query": "How does the pH scale work?",
  "concept": "ph_scale",
  "degraded": false,
  "attempts": 1,
  "gates": [
    {"gate": "G1", "passed": true, "attempt": 1},
    {"gate": "G4", "passed": false, "attempt": 1, "reason": "missing anchor: logarithmic"},
    {"gate": "G4", "passed": true,  "attempt": 2}
  ],
  "scenes": [{"scene_id": "s1", "visual": "ph_scale_bar", "audio_s": 12.8, "frames": 384}],
  "video": {"duration_s": 68.4, "size_bytes": 7930112, "has_audio": true, "has_video": true},
  "cost": {"llm_usd": 0.0011, "tts_usd": 0.0, "total_usd": 0.0011,
           "production_estimate_usd": 0.0181},
  "timings": {"scripting": 4.3, "narrating": 8.7, "rendering": 11.2, "muxing": 4.4},
  "model": "gemini-2.5-flash-lite",
  "created_at": "..."
}
```

One artifact serving three graded criteria: architecture (a clean artifact boundary),
reliability (evidence the gates fired), observability (a named Quality sub-criterion).

---

## 13. Visual design contract

Cost per additional unit of visual quality here is **zero** — the work is done once and
applies to every concept and every run. This is where "cheapest reasonable cost" is
actually earned.

- **No matplotlib defaults.** `theme.py` sets rcParams: 3-colour palette plus a neutral,
  one font family, no spines, faint grid, consistent margins.
- **Fixed layout grid** across every scene: heading zone (top), content zone (middle),
  caption zone (bottom). Shared grid makes the video read as designed rather than
  assembled.
- **Title card shows the learner's original query verbatim.** Visual proof of R6.
- **Burned-in captions.** The current scene's narration is wrapped and rendered in the
  caption zone. Highest value-per-effort item in this section: it improves perceived
  quality, adds accessibility, and lets an evaluator follow the demo **with the sound
  off**.
- **0.3s crossfades** between scenes. Hard cuts between static frames read as a
  slideshow.
- **Animation on exactly two types** (§7) — the ones where motion carries meaning.
- **`loudnorm` plus 0.4s trailing silence per scene.** Educational narration without
  pauses feels rushed.
- Output: 1280×720, 30fps, ~800 kbps (flat graphics need no more).

---

## 14. Cost model

Assumptions: ~1,500 input tokens, ~1,000 output tokens per script call, mean 1.3 calls
per job; ~1,000 characters of narration; ~45s CPU; ~8 MB artifact.

| Item | Dev (free tier) | Production | Basis |
|---|---|---|---|
| LLM script | $0 | **$0.0007** | `gemini-2.5-flash-lite` $0.10 / $0.40 per 1M |
| TTS | $0 | **$0.0160** | Azure Neural TTS $16 per 1M chars |
| Render + encode | ~$0 | **$0.0005** | ~45s CPU @ $0.04/vCPU-hr |
| Storage | $0 | **$0.0002/mo** | 8 MB @ $0.023/GB-mo |
| Egress per view | $0 | **$0.0007** | 8 MB @ $0.09/GB |
| **Total** | **$0** | **≈ $0.018** | |

**Generative video comparison:** $0.10–0.40 per second × 75s = **$7.50–30 per video**,
i.e. 400–1600× more. But the stronger argument is not price: those APIs cap at 8–10s
clips, so one 75s video means 8–10 independently non-deterministic generations that must
be stitched and kept stylistically consistent. Programmatic rendering **eliminates** that
non-determinism source instead of managing it.

**Finding worth reporting: TTS is ~89% of production cost; the LLM is ~4%.** This is
counter-intuitive and it changes what to optimise. The highest-leverage lever is caching
narration audio by `hash(text + voice)`, not caching LLM calls. If costs must fall
further, the move is self-hosted TTS (Piper, Kokoro), not a cheaper LLM.

Free-tier caveats to state in the README: Gemini's free tier is Flash/Flash-Lite only at
roughly 5–15 RPM and ~1,000–1,500 requests/day, and free-tier data may be used to
improve Google's products — acceptable for a prototype, not for real learner data.

---

## 15. Reliability acceptance

`scripts/harness.py` runs each concept N times (N ≥ 5, target 10) and emits
`reports/reliability.md`:

| Concept | Runs | First-attempt pass | Needed retry | Degraded | Failed | Duration min/med/max | Mean cost |
|---|---|---|---|---|---|---|---|

Plus **per-gate failure counts**. That table is what proves each guardrail earns its
place. If G4 never fires across 30 runs, say so and state the conclusion — either the
anchors are too loose or the gate is redundant. Honesty there is stronger evidence of
engineering judgement than pretending every gate was essential.

Acceptance criteria:

1. `failed` count = 0 across all runs (infrastructure failures excepted and reported).
2. Every completed video passes G7.
3. Every completed video's script satisfies all of its concept's anchors — by
   construction, since G4 gates it or the fallback is used.
4. Duration always within 45–90s.
5. `degraded` rate reported, not hidden.

Note the apparent tension with the brief, which asks for the "three best" videos while
also saying it does not judge on the best single output. The resolution is to submit
three best runs **plus** this distribution: *here are the best three, and here is
evidence the worst run is not far behind.*

Operational note: at 5–15 RPM the harness must run sequentially with throttling —
roughly 10–15 minutes per full pass. Run it in the background, not during recording.

---

## 16. Tradeoffs and what was faked

| Decision | Rationale | Cost of the tradeoff |
|---|---|---|
| Programmatic rendering | Removes a non-determinism source; ~400–1600× cheaper | Visual variety is bounded by the type library |
| Closed `VisualType` enum | Makes G3 possible; guarantees renderability | A new visual needs code, not just a prompt change |
| Rule-based resolver | Deterministic, free, injection-safe | Paraphrases outside the alias sets are rejected |
| Query never enters the prompt | Injection-safe; stable content per intent | Cannot personalise to phrasing |
| In-memory persistence | Permitted; boundary is clean | Restart loses jobs |
| Local artifact store | Permitted; boundary is clean | Single-node only |
| asyncio + semaphore(2) | ffmpeg is CPU-bound; unbounded concurrency manufactures flakiness that looks like pipeline failure | No cross-process durability |
| Fixed 2 retries | Bounded latency and cost; fallback covers the tail | A transient 3rd-attempt success is missed |
| Animation on 2 types only | Motion where it carries meaning; static elsewhere | Less dynamic overall |
| Burned-in captions, no SRT | Works everywhere, demo-able without audio | Not selectable or translatable |
| Single en-US voice | One less variable | No localisation |
| edge-tts | Free, no key, good neural quality | **Unofficial endpoint** — can be rate-limited or broken. Mitigated by retry, caching, and the `TTSProvider` seam |
| Fallback scripts | Makes the script stage unfailable | Degraded runs are less varied — always flagged |

**Faked, behind clean seams:** persistence, artifact store, queue, cost from a static
price table, LLM and TTS in tests. Each is one class away from real.

**Left out entirely:** see `CLAUDE.md` §2.
