"""Script retry policy and the fallback — SPEC.md §9.1.

**This stage cannot fail.** Three attempts at the model, then a pre-committed
script that has already passed every gate. `script_unavailable` exists for a
missing fallback file, which is a deployment error rather than a runtime one.

That guarantee is what makes the reliability claim in SPEC.md §15 testable:
every job with a resolvable concept reaches `completed` unless TTS or ffmpeg
fails at the infrastructure level. Nothing the model does can stop a learner
getting a video.

**Why the retries earn their place (R8).** A blank retry is a second lottery
ticket. This one is not blank: the failing gate, the specific missing anchor
and what was expected are rendered into `prompts/retry.md.j2` and appended to
attempt 2 and 3. The model is told what was wrong, which is the difference
between engineering and hoping. Retries stop at three because a model that has
missed the same anchor twice with explicit feedback is not going to find it on
the fourth ask, and the fallback is already good.

The fallback is a **declared, flagged** degradation path, never a silent one:
`degraded=true` travels all the way to the API response and the manifest
(CLAUDE.md §11).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.concepts.registry import ConceptContract
from app.domain.script import Script
from app.logging import get_logger
from app.pipeline.cost import CostTracker, LLMUsage
from app.pipeline.gates import GateFailure, run_script_gates
from app.providers.llm import LLMProvider

log = get_logger(__name__)

GATE_ORDER = ("G1", "G2", "G3", "G4")
"""Script gates, in the order `run_script_gates` runs them.

Needed because that function returns only the *first* failure. Since the gates
stop at the first failure, every gate before the reported one necessarily
passed - so the full per-attempt picture is recoverable from one result, and
the manifest can show it without the gate runner having to carry a log.
"""


class ScriptUnavailable(RuntimeError):
    """The fallback file is missing or unusable. Maps to `script_unavailable`.

    Deliberately the only way this module can raise. If it ever fires, the
    deployment is broken - the file is committed to the repository.
    """


@dataclass(frozen=True)
class GateAttempt:
    """One gate's verdict on one attempt. Serialised into `manifest.json`."""

    gate: str
    passed: bool
    attempt: int
    reason: str | None = None

    def as_dict(self) -> dict:
        record = {"gate": self.gate, "passed": self.passed, "attempt": self.attempt}
        if self.reason:
            record["reason"] = self.reason
        return record


@dataclass(frozen=True)
class ScriptOutcome:
    script: Script
    degraded: bool
    attempts: int
    """Model calls consumed. Zero when the provider was never reachable."""
    gates: tuple[GateAttempt, ...]
    model: str | None = None
    last_failure: GateFailure | None = None
    """Why the fallback was needed. None on a clean run."""


def load_fallback(concept: ConceptContract) -> Script:
    """The pre-committed script for this concept.

    Re-validated through the gates on every load rather than trusted. It is
    committed to the repository and covered by tests, but a fallback that
    silently drifted out of contract would fail at the renderer instead of
    here, where the reason is still legible.
    """
    try:
        raw = concept.fallback_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScriptUnavailable(
            f"No fallback script for {concept.key} at {concept.fallback_path}"
        ) from exc

    result = run_script_gates(raw, concept)
    if not result.ok:
        raise ScriptUnavailable(
            f"The committed fallback for {concept.key} does not pass its own "
            f"gates: {result.failure.gate}/{result.failure.reason} - "
            f"{result.failure.detail}"
        )
    return result.script


def _record_attempt(failure: GateFailure | None, attempt: int) -> list[GateAttempt]:
    """Expand one gate result into a row per gate that actually ran."""
    if failure is None:
        return [GateAttempt(gate, True, attempt) for gate in GATE_ORDER]

    rows: list[GateAttempt] = []
    for gate in GATE_ORDER:
        if gate == failure.gate:
            rows.append(GateAttempt(gate, False, attempt,
                                    f"{failure.reason}: {failure.detail}"))
            break
        rows.append(GateAttempt(gate, True, attempt))
    return rows


def resolve_script(
    llm: LLMProvider,
    concept: ConceptContract,
    tracker: CostTracker,
    *,
    max_attempts: int = 3,
) -> ScriptOutcome:
    """Generate, gate, retry with feedback, fall back. Never raises on model failure."""
    gates: list[GateAttempt] = []
    feedback: GateFailure | None = None
    model: str | None = None
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        try:
            raw = llm.generate_script(concept, feedback)
        except Exception as exc:  # noqa: BLE001
            # Not swallowed: it is counted as a failed attempt, logged with the
            # exception, and drives the same feedback path as a gate failure.
            # A provider outage and a malformed script are the same problem
            # from here - no usable script - and both end at the fallback.
            attempts += 1
            failure = GateFailure("G1", "provider_error", repr(exc))
            gates.extend(_record_attempt(failure, attempt))
            feedback = failure
            log.warning("script.provider_failed", concept=concept.key,
                        attempt=attempt, error=repr(exc))
            continue

        attempts += 1
        model = raw.model or model
        tracker.record_llm_call(LLMUsage(
            prompt_tokens=raw.prompt_tokens,
            output_tokens=raw.output_tokens,
            thinking_tokens=raw.thinking_tokens,
        ))

        result = run_script_gates(raw.text, concept)
        gates.extend(_record_attempt(result.failure, attempt))

        if result.ok:
            log.info("script.accepted", concept=concept.key, attempt=attempt,
                     words=result.script.total_words)
            return ScriptOutcome(script=result.script, degraded=False,
                                 attempts=attempts, gates=tuple(gates),
                                 model=model)

        feedback = result.failure
        log.warning("script.gate_failed", concept=concept.key, attempt=attempt,
                    gate=result.failure.gate, reason=result.failure.reason)

    log.warning("script.degraded", concept=concept.key, attempts=attempts,
                last_gate=feedback.gate if feedback else None)
    return ScriptOutcome(
        script=load_fallback(concept),
        degraded=True,
        attempts=attempts,
        gates=tuple(gates),
        model=model,
        last_failure=feedback,
    )
