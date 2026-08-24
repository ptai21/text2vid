# Architecture

Why the boundaries fall where they do. Contracts live in [SPEC.md](SPEC.md); this
document is the reasoning behind them.

---

## The shape of the thing

```
POST /videos
    │
    ▼
 resolver ──── rule-based, no LLM ────▶ 400 unsupported_concept  (before any spend)
    │
    ▼  ConceptKey
 ┌──────────────────────────────────────────────────────────────┐
 │  JobRunner        asyncio task + Semaphore(2) + wait_for     │
 │      │                                                       │
 │      ▼                                                       │
 │  Orchestrator     sequences stages, owns failure naming      │
 │      │                                                       │
 │      ├─ scripting   LLMProvider ──▶ G1 G2 G3 G4 ──▶ retry ──▶ fallback
 │      ├─ narrating   TTSProvider ──▶ G5a G5b                  │
 │      ├─ rendering   VisualProvider ──▶ G6                    │
 │      ├─ muxing      ffmpeg ──▶ G7                            │
 │      └─ publishing  ArtifactStore                            │
 └──────────────────────────────────────────────────────────────┘
    │
    ▼
 artifacts/{job_id}/  video.mp4 · script.json · manifest.json
```

`POST` returns `202` the moment the job is persisted. Everything after that is a
background `asyncio.Task` — the client learns about it through `GET /videos/{job_id}`.

### Why `asyncio` and not Celery

The out-of-scope list rules out a broker, but the choice would be the same without it.
The workload is one CPU-bound stage (ffmpeg — 35.4s of a 46s job while the encoder still
zoomed, and roughly half a job's 27.6s once it did not) wrapped in two I/O-bound ones. A
`Semaphore(2)` bounds the CPU contention that actually matters; a broker would add
a process boundary, a serialisation format and a failure mode without addressing it. What
a broker *would* buy is durability across restart — and that is listed as a known limit
rather than pretended away.

Two implementation details that are easy to get wrong and were:

- **Tasks are held in a strong reference set.** Without it the event loop can garbage
  collect a running task mid-flight, which looks exactly like a job silently vanishing.
- **`queue_depth` counts jobs waiting on the semaphore, not jobs unfinished.** A running
  job is not queued. Conflating the two makes `/health` report load it is coping with as
  though it were backlog.

---

## Job lifecycle

```
queued ──▶ running ──▶ completed     (terminal)
   │          │
   └──────────┴──────▶ failed        (terminal)
```

Stage only advances: `resolving → scripting → narrating → rendering → muxing →
publishing`.

The transition table is a **guard, not a convention**. It lives in `app/domain/state.py`,
imports nothing, and raises `InvalidTransition` on an illegal move — logged, never
silently ignored. Three invariants it exists to protect:

1. `completed` requires a non-null artifact **and** a passed G7.
2. `failed` requires a non-null `failure`.
3. Terminal states never transition again.

`degraded=true` is orthogonal. It coexists with `completed` because it is a quality flag,
not a failure — the learner still gets a correct, watchable video, and the operator still
gets told the model did not produce it.

### Failures are named at the point they are known

The orchestrator raises `StageFailure(code, stage, message, detail)`; the runner records
the code on the job. This exists because the first version hardcoded `internal_error` for
everything, which left five of the then-nine `FailureCode` values as dead code and made R7
("failures are named and explicit") true only on paper.

The last unnamed one was the timeout, found while fact-checking the README against the
code: `JobRunner` caught `asyncio.TimeoutError` and called `_fail` without a code, so it
took the `internal_error` default. The message and the log line were always right — only
the machine-readable field was wrong, which is the harder kind to notice. `timeout` is now
its own code, and the generic catch-all genuinely means *bug*, which is the only thing it
should ever mean.

---

## Boundary 1 — persistence and artifacts

```python
class JobRepository(Protocol):
    async def create(self, job: Job) -> None: ...
    async def get(self, job_id: str) -> Job | None: ...
    async def list(self, *, status=None, concept=None, limit=20, offset=0) -> tuple[list[Job], int]: ...
    async def update(self, job: Job) -> None: ...

class ArtifactStore(Protocol):
    def put(self, job_id: str, name: str, src: Path) -> str: ...
    def open(self, job_id: str, name: str) -> BinaryIO: ...
    def exists(self, job_id: str, name: str) -> bool: ...
```

`InMemoryJobRepository` and `LocalArtifactStore` are the implementations. Swapping either
for Postgres or S3 is one new class and one line in `app/main.py`.

**The repository is async and the artifact store is not**, and the asymmetry is
deliberate. `InMemoryJobRepository` guards writes with an `asyncio.Lock` because two jobs
run concurrently, and a real repository would do network I/O here anyway — so async
signatures mean swapping it never touches a call site. `ArtifactStore` does small local
file operations that the generator already runs off the event loop; making it async would
be ceremony that buys nothing.

This boundary was quietly voided once and had to be repaired. The stub publisher called
`LocalArtifactStore.write_text`, a method the Protocol did not declare — so it depended on
the concrete class, and "swapping is a one-class change" had become false without anything
failing. Publishing now writes a temp file and calls `put()`. **A seam that is not exercised
through its own interface is not a seam.**

---

## Boundary 2 — AI and video generation

```python
class LLMProvider(Protocol):
    def generate_script(self, concept: ConceptContract,
                        feedback: GateFailure | None) -> RawScript: ...

class TTSProvider(Protocol):
    async def synthesize(self, text: str, out: Path) -> AudioResult: ...

class VisualProvider(Protocol):
    def render(self, scene: Scene, duration: float, out_dir: Path) -> list[Path]: ...
```

Everything external is behind one of these three. Gemini, edge-tts and ffmpeg are
reachable **only** through `app/providers/` — there is no SDK import and no `subprocess`
call anywhere else in the tree. This is enforced by review rather than by tooling, and it
is the concrete answer to R9 ("it must be obvious where a real provider plugs in"):

| Swap to | Work |
|---|---|
| OpenAI / Anthropic instead of Gemini | one `LLMProvider` |
| Azure Neural TTS instead of edge-tts | one `TTSProvider` |
| A generative video API instead of matplotlib | one `VisualProvider` |

The third one is worth dwelling on, because it is where the interesting decision is.

### Why the LLM produces a script and never pixels

The settled choice is programmatic rendering. The cost argument is the obvious one —
$0.018 against $7.50–30 per video, 400–1600× — but it is the weaker half.

Generative video APIs cap at 8–10 second clips. A 75-second video therefore means **8–10
independently non-deterministic generations**, stitched together and hoped to stay
stylistically consistent, with no way to validate any of them beyond looking. That is not
one unreliable stage; it is eight, in series, in the layer furthest from anything a test
can assert about.

Confining the LLM to a structured script does the opposite. There is exactly one
non-deterministic stage, it emits JSON, and JSON can be gated. The `VisualType` enum is
**closed** precisely so G3 can guarantee that whatever the model asks for can actually be
drawn — the model chooses *which* visual and supplies its parameters; it never chooses
what a visual is.

The tradeoff is real and gets stated rather than buried: **visual variety is bounded by
the type library, and a new visual needs code rather than a prompt change.**

### Why the learner's query never enters the prompt

The prompt has three layers — invariant system rules, the concept contract from the
registry, and (on a retry) the gate feedback. The raw query is in none of them.

This is a structural guarantee rather than a filter. Prompt injection has nothing to
inject into, and rephrasing cannot move the content, because the only thing that crosses
from the learner into generation is a `ConceptKey` chosen by rule. The query is not
discarded — it is stored in job metadata and printed on the title card, so the video is
visibly an answer to *their* question (R6).

That last part was nearly lost. `title_card` was in every concept's `allowed_visuals` but
in none of its `required_visuals`, and a live `ionic_vs_covalent` run opened on
`electron_transfer` instead — a perfectly valid video that never showed the learner their
own question. All 325 tests were green before and after. It was caught by reading a
`manifest.json`. **Tests prove the rules are enforced; only looking at the artifact proves
the rules are right.**

---

## Boundary 3 — layers

```
api/          →  pipeline/  →  providers/ , storage/
                     ↓              ↓
                  domain/  ←────────┘         (imports nothing)
concepts/     data, not logic
```

- `api/` holds no business logic: validate, call the pipeline, map to HTTP.
- `domain/` imports nothing from `api/`, `pipeline/`, `providers/` or `storage/`. It is
  pure Python — the job model, the state machine, the script schema — and it is testable
  without a single mock.
- `pipeline/` never imports FastAPI and never touches an HTTP concept.
- `concepts/` is data. The moment logic accumulates there it belongs in `pipeline/`.

The registry is where extensibility lives. A fourth STEM topic is one `ConceptContract`
entry — canonical question, aliases, required anchors, allowed and required visuals,
fallback script — with no pipeline change and no prompt change. `GET /concepts` exposes
that extension point to a reader of the API alone. The scope lock says three concepts; the
registry is how the fourth is demonstrated without building it.

---

## Audio-first timing

TTS runs before rendering. Each clip is measured with `ffprobe`, and those measurements
drive the visual timing — so synchronisation is a *consequence* of the ordering rather
than a calibration task with a fudge factor in it.

The arithmetic is exact rather than approximately right. N stills joined by N−1
crossfades of length T play for `sum − (N−1)·T`, so each still after the first is held T
longer and the total lands back on the measured narration length:

```
audio          11.3  13.1  13.3  14.6  12.6   →  63.3s
stills         11.3  13.5  13.7  15.0  13.0   (each after the first +0.4)
xfade offsets  10.9  23.6  36.5  50.7
video total                                       63.3s
```

Verified on three full runs: expected 63.3 / 60.4 / 68.5s → actual 63.3 / 60.5 / 68.5s.
`tests/test_render.py` asserts both this and the naive version it replaces, so a
regression names itself instead of drifting the video 1.6 seconds short.

**Five renders per video, not one per frame.** An earlier reading of the spec implied
per-scene animation at ~900 `savefig` calls per video; five is a thousandfold less work.
The only movement is the encoder's 0.4s crossfades.

There was a subtle `zoompan` too, until watching a finished video showed it trembling.
`zoompan` crops in whole input pixels, and a 4% ramp over a 12-second scene moves its crop
origin 0.136px per frame — so the crop holds for seven frames, jumps one, holds for eight,
jumps again. The *unevenness* is what the eye catches; a steady drift would have been
invisible. The 2× upscale that was supposed to fix this only halved the amplitude, and the
code comment claiming it was smooth had never been checked against a number.

It is not tunable. One pixel per frame needs a 14.6× source; a 2× source needs a 39% zoom
that would crop the captions off. **A subtle zoom and `zoompan` are incompatible by
construction**, so the zoom is gone — which also made the encode 2.2× faster. This is the
build's clearest case of a mitigation that was reasoned correctly and never measured.

---

## Build · fake · simplify · leave out

| | What | Why |
|---|---|---|
| **Built for real** | The gates, the retry-with-feedback loop, the state machine, audio-first timing, all eight scene renderers, the ffmpeg pipeline, cost metering from real token counts | This is where the grading is. Reliability under non-determinism cannot be faked, and neither can a cost model |
| **Faked behind a clean seam** | Persistence (in memory), artifact store (local files), the queue (`asyncio` + semaphore), cost (static price table), LLM and TTS in tests (recorded fixtures) | Each is one class from real, and each seam is exercised by the code that uses it — not just declared |
| **Simplified** | 8 visual types not 30 · one en-US voice · burned-in captions instead of SRT · animation on two visual types only · fixed 2 retries | Every one of these trades variety for a smaller surface that can be validated |
| **Left out** | Frontend, auth, database, broker, Docker, CI, cache layer, webhooks, job cancellation, range requests, a fourth concept | `CLAUDE.md` §2. Building any of them would have come out of the reliability budget |

The recurring shape: **prefer removing a source of non-determinism to managing one.** That
is the same decision made three times — programmatic rendering over generative video, a
rule-based resolver over an LLM router, and a closed enum over free-form visual
descriptions.

---

## Known risks

| Risk | Standing |
|---|---|
| **edge-tts is an unofficial endpoint** | Real. Can be rate-limited or broken without notice. Mitigated by explicit timeout, 3× backoff, a sha256 audio cache, and the `TTSProvider` seam. Acceptable for a prototype, not for production without a paid TTS behind the same interface |
| **Gemini free-tier data may be used to improve Google's products** | Real. 5–15 RPM and ~1,000–1,500 requests/day besides. Fine for this; not acceptable for real learner data. A paid tier changes the terms, not a line of code |
| **In-memory jobs are lost on restart** | Accepted, and the reason `JobRepository` is async today |
| **Single node artifact storage** | Accepted, same seam |
| **G2's word budget binds at the floor, not the ceiling** | The anticipated risk was the top: 190 words at the slowest observed pace ≈ 89.0s against a 90s ceiling. It never materialised across 30 runs — longest video 78.5s. The real case is the other end: **four `total_words` rejections, all low** (113, 115, 121 words against a 125 minimum), seen in the harness, in `tests/test_contracts.py` and in demo runs alike. The model writes short. `MAX_TOTAL_WORDS` is **deliberately left at 190** — narrowing a threshold that has never once bound would be a speculative edit dressed as a fix |
| **Fifteen runs is a distribution, not a guarantee** | Nothing here is proven about a fourth concept, a different model, or sustained load |

---

## Where the mistakes are written down

[reports/findings.md](reports/findings.md) records every bug, every contradiction between
the three governing documents, and every decision that was escalated rather than guessed.
It is kept because the brief grades *AI-agent workflow* — visible planning, verifiable
steps, inspected output — and a build that never records its own mistakes cannot evidence
any of that.

The two most useful entries are both bugs that **the test suite could not see**: the empty
`params` from a schema mistake, and the missing title card. Both happened with 325 tests
green. Both were caught by looking at a real artifact.
