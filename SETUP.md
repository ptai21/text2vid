# SETUP.md

From-zero setup, environment verification, and run instructions.
Personal operating guide — **not** part of the submission bundle.
Behaviour lives in `SPEC.md`, build order in `PLAN.md`, agent rules in `CLAUDE.md`.

---

## 1. Prerequisites

Run every check from **Git Bash**, not PowerShell — the two resolve `PATH` differently
on Windows and ffmpeg is the usual casualty.

```bash
python --version     # 3.11.8
uv --version
git --version
ffmpeg -version
ffprobe -version
```

If ffmpeg is missing:

```bash
winget install Gyan.FFmpeg
# then reopen Git Bash and re-check
```

`ffprobe` must be present too — G5 and G7 both depend on it. Installing `ffmpeg`
without `ffprobe` is a common and confusing failure.

---

## 2. Project bootstrap

```bash
cd /c/text2vid

uv init --python 3.11.8 --no-workspace
uv add fastapi "uvicorn[standard]" pydantic pydantic-settings \
       google-genai edge-tts matplotlib jinja2 structlog
uv add --dev pytest pytest-asyncio httpx
uv sync
```

Nothing else gets added without an explicit decision (`CLAUDE.md` §5). If the agent
proposes a package mid-session, that is a stop-and-ask moment.

### Directory skeleton

```bash
mkdir -p app/{api,domain,concepts/fallbacks,pipeline,providers/scenes,storage} \
         prompts scripts tests/fixtures/llm artifacts submissions reports

find app -type d -exec touch {}/__init__.py \;
touch app/__init__.py scripts/__init__.py tests/__init__.py
```

---

## 3. Configuration

`.env.example` — committed:

```
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
TTS_VOICE=en-US-AriaNeural
TTS_RATE=+0%
MAX_CONCURRENT_JOBS=2
MAX_SCRIPT_ATTEMPTS=3
JOB_TIMEOUT_S=600
ARTIFACT_DIR=./artifacts
VIDEO_WIDTH=1280
VIDEO_HEIGHT=720
VIDEO_FPS=30
LOG_LEVEL=INFO
```

```bash
cp .env.example .env
# paste your key from https://aistudio.google.com/apikey
```

The Gemini free tier needs no credit card. Note two caveats worth a line in the README:
free-tier traffic runs at roughly 5–15 requests per minute, and free-tier data may be
used to improve Google's products — fine for a prototype, not for real learner data.

### `.gitignore`

```
.venv/
__pycache__/
*.pyc
.env
artifacts/
reports/*.html
.pytest_cache/
*.mp4
!submissions/**/*.mp4
```

The last two lines matter: generated videos stay out of git, but the three submitted
runs under `submissions/` are committed — the brief asks for them specifically so it can
track exactly what the system produced at submission time.

### `pyproject.toml` — pytest config

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = ["slow: touches real network or ffmpeg"]
addopts = "-m 'not slow'"
```

`uv run pytest -q` then runs only the fast suite by default. Real-I/O contract tests run
explicitly with `uv run pytest -m slow`.

---

## 4. Environment smoke test — run this first

`scripts/smoke.py` checks the four external dependencies **independently**. Discovering
four separate failure modes tangled together mid-session is the most common way this
challenge goes wrong.

```bash
uv run python -m scripts.smoke
```

Expected:

```
[1/4] gemini ......... ok   (gemini-2.5-flash-lite, 0.9s)
[2/4] edge-tts ....... ok   (2.4s audio, 18KB)
[3/4] matplotlib ..... ok   (1280x720 png)
[4/4] ffmpeg mux ..... ok   (2.5s mp4, video+audio streams)

all checks passed
```

**Do not start the recorded session until all four pass.**

Check 1 also prints the resolved `google-genai` call signature. Confirm it against what
`SPEC.md` §8 assumes — the SDK's shape has changed across versions, and this is the
single highest-risk unknown going into the session.

---

## 5. Running the service

```bash
uv run uvicorn app.main:app --reload
```

- Swagger UI: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`

Manual walkthrough:

```bash
# submit
curl -s -X POST localhost:8000/videos \
  -H 'Content-Type: application/json' \
  -d '{"query":"How does the pH scale work?"}'

# poll
curl -s localhost:8000/videos/<job_id>

# list
curl -s 'localhost:8000/videos?status=completed'

# fetch the artifact
curl -s -o out.mp4 localhost:8000/videos/<job_id>/artifact

# run record
curl -s localhost:8000/videos/<job_id>/manifest
```

---

## 6. Demo walkthrough

`scripts/demo.py` submits all three required concepts, polls until each finishes,
prints the state transitions, and writes a summary table. It is simultaneously the
end-to-end smoke test, the demo you record, and the "API walkthrough" deliverable.

```bash
uv run python -m scripts.demo
```

Uses only the standard library plus `httpx` — no `jq`, which is usually absent from
Git Bash.

---

## 7. Tests

```bash
uv run pytest -q                      # fast suite (T1+T2), target under 3s
uv run pytest tests/test_gates.py -q  # the gate corpus
uv run pytest -m slow                 # real network + ffmpeg contract tests
```

Write `tests/test_gates.py` and `tests/fixtures/llm/` **before** the session
(`PLAN.md` §0.5). They derive directly from `SPEC.md` §6 and §10 and need no agent —
and having them red at the start of round 5 is what makes that round worth recording.

---

## 8. Reliability harness

```bash
uv run python -m scripts.harness --runs 5     # 3 concepts x 5 = 15 runs
```

Writes `reports/reliability.md`. Roughly 10–12 minutes at 5 runs, dominated by TTS and
encoding, with sequential pacing to respect the free-tier rate limit.

Start it, then write `README.md` and `ARCHITECTURE.md` while it runs. Good use of the
wall clock, and it reads well on the recording.

---

## 9. Pre-flight checklist before recording

- [ ] `uv run python -m scripts.smoke` — all four green
- [ ] `google-genai` call signature confirmed against `SPEC.md` §8
- [ ] `tests/test_gates.py` written and **failing** for the right reason
- [ ] Fixture corpus in `tests/fixtures/llm/` complete
- [ ] Docs committed (`CLAUDE.md`, `SPEC.md`, `PLAN.md`, tests, fixtures)
- [ ] `git log` shows those commits timestamped **before** any implementation
- [ ] Free disk ≥ 15 GB (a 4-hour 720p recording runs 4–7 GB)
- [ ] Zoom: local recording on, screen + camera, 60-second test clip **played back**
- [ ] Notifications silenced, secrets and unrelated tabs closed
- [ ] `.env` present but **not** on screen when you share

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ffmpeg: command not found` in Git Bash only | PATH set for PowerShell | Reopen Git Bash after install; check `which ffmpeg` |
| matplotlib hangs / opens a window | Interactive backend | `matplotlib.use("Agg")` before importing pyplot |
| ffmpeg exits 1 with no message | `stderr` discarded | Always capture `stderr`; it becomes `failure.detail` |
| edge-tts hangs | Unofficial endpoint, no default timeout | Hard 30s timeout + backoff in `TTSProvider` |
| Gemini 429 | Free-tier rate limit | Space calls; harness must run sequentially |
| Gemini 404 on model | Stale model string | `gemini-2.5-flash-lite`. **Gemini 2.0 Flash was shut down 2026-06-01** |
| mp4 plays silent | Audio stream absent | That is exactly what G7 exists to catch — check ffprobe output |
| Paths break inside JSON | Windows backslashes | Normalise with `Path.as_posix()` before serialising |
| Job stuck in `running` | Exception escaped the task | Top-level try/except → `internal_error`; plus `JOB_TIMEOUT_S` |

---

## 11. Submission

- [ ] `README.md` — setup, run, API, **test instructions**, cost model, what you
      optimised for, how flaky generation is avoided
- [ ] `ARCHITECTURE.md` — separate file: job lifecycle, persistence/artifact boundary,
      AI/video-generation boundary, the build/fake/simplify/leave-out table, known risks
- [ ] Three best videos under `submissions/`, each with its originating query,
      `script.json`, and `manifest.json`; filenames embed the `job_id`
- [ ] Demo recording covering all three concepts
- [ ] GitHub read access for `careers@growtrics.ai`, `praveen.k@growtrics.ai`,
      `wayne.le@growtrics.ai`
- [ ] Zip excluding `.venv/`, `__pycache__/`, `artifacts/`
- [ ] Google Drive link — sharing verified **and the file confirmed playable**
- [ ] Email to `careers@growtrics.ai`, CC `praveen.k@` and `wayne.le@`
