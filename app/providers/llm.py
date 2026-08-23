"""LLM provider and prompt construction — SPEC.md §8 and §11.

Three prompt layers, two of them files under `prompts/` rather than f-strings:
layer 1 is invariant, layer 2 is rendered from the concept registry, layer 3
is gate feedback appended only on a retry.

A retry without feedback is a second lottery ticket. A retry that names the
failing gate and the missing anchor is engineering — that is the justification
R8 demands for the retry guardrail.

**The learner's raw query never enters the prompt.** Layer 2 uses the
registry's `canonical_question`, not what the learner typed (CLAUDE.md §4).
That blocks prompt injection and stops phrasing variance from moving the
content; the raw query is stored on the job and rendered on the title card,
which is what satisfies R6.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.concepts.registry import ConceptContract
from app.config import Settings

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

SYSTEM_TEMPLATE = "system.md"
CONCEPT_TEMPLATE = "concept.md.j2"
RETRY_TEMPLATE = "retry.md.j2"

# Human-readable form of the parameter contract that G3 enforces. It lives
# here rather than in `pipeline/gates.py` because `providers/` may not import
# `pipeline/` (CLAUDE.md §8, dependency direction). `tests/test_prompt.py`
# asserts these keys stay in step with `VISUAL_PARAMS`, so the two cannot
# drift apart unnoticed.
VISUAL_PARAM_DOCS: dict[str, str] = {
    "title_card": "no parameters; the query and concept title are added by the renderer",
    "ph_scale_bar": "markers: up to 4 numbers, each between 0 and 14",
    "log_steps": "from_ph: integer 0-14, to_ph: integer 0-14",
    "atom_pair": "left: element symbol, right: element symbol, "
                 "shared_pairs: integer 1-3",
    "energy_curve": "min_distance: number, label: short text",
    "electron_transfer": "donor: element symbol, acceptor: element symbol",
    "side_by_side_comparison": "left_title: text, right_title: text, "
                               "rows: 2-4 pairs of [left, right] text",
    "summary_card": "points: 2-4 short strings, each at most 10 words",
}


# The same contract again, as JSON Schema fragments for `responseSchema`.
#
# A generic `{"type": "object"}` for `params` does not work: constrained
# generation emits only what the schema declares, so an object with no
# declared properties comes back empty and every scene fails G3 on a missing
# param. Naming the properties is what lets the model fill them.
_NUMBER = {"type": "number"}
_INTEGER = {"type": "integer"}
_STRING = {"type": "string"}

VISUAL_PARAM_TYPES: dict[str, dict[str, dict]] = {
    "title_card": {},
    "ph_scale_bar": {"markers": {"type": "array", "items": _NUMBER}},
    "log_steps": {"from_ph": _INTEGER, "to_ph": _INTEGER},
    "atom_pair": {"left": _STRING, "right": _STRING, "shared_pairs": _INTEGER},
    "energy_curve": {"min_distance": _NUMBER, "label": _STRING},
    "electron_transfer": {"donor": _STRING, "acceptor": _STRING},
    "side_by_side_comparison": {
        "left_title": _STRING,
        "right_title": _STRING,
        "rows": {"type": "array", "items": {"type": "array", "items": _STRING}},
    },
    "summary_card": {"points": {"type": "array", "items": _STRING}},
}


class GateFeedback(Protocol):
    """Structural stand-in for `pipeline.gates.GateFailure`.

    Typed structurally rather than imported so that `providers/` keeps its
    dependency direction. `GateFailure` satisfies this without knowing it
    exists.
    """

    gate: str
    reason: str
    detail: str


@dataclass(frozen=True)
class RawScript:
    """Untrusted model output plus what it cost to obtain.

    `thinking_tokens` is tracked separately because it is invisible in
    `text` and still billed at the output rate (SPEC.md §14).
    """

    text: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    model: str | None = None


class LLMProvider(Protocol):
    def generate_script(
        self, concept: ConceptContract, feedback: GateFeedback | None = None
    ) -> RawScript: ...


class PromptBuilder:
    def __init__(self, prompts_dir: Path = PROMPTS_DIR):
        self._dir = prompts_dir
        self._env = Environment(
            loader=FileSystemLoader(prompts_dir),
            undefined=StrictUndefined,  # a typo in a template must fail loudly
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def system(self) -> str:
        return (self._dir / SYSTEM_TEMPLATE).read_text(encoding="utf-8")

    def build(
        self, concept: ConceptContract, feedback: GateFeedback | None = None
    ) -> str:
        parts = [
            self.system(),
            self._env.get_template(CONCEPT_TEMPLATE).render(
                concept=concept, param_docs=VISUAL_PARAM_DOCS
            ),
        ]
        if feedback is not None:
            parts.append(
                self._env.get_template(RETRY_TEMPLATE).render(feedback=feedback)
            )
        return "\n\n".join(part.strip() for part in parts) + "\n"


def response_schema(concept: ConceptContract) -> dict:
    """The `responseSchema` for a constrained generation (SPEC.md §7).

    The visual type enum is narrowed to this concept's allowed list, so the
    cheapest way to fail G3 is closed off at the source. G1 and G3 still run:
    structured output is a constraint, not a guarantee.

    `params` declares the union of parameters across this concept's visuals,
    all optional, since one object has to serve every type. Narrowing it to
    the concept rather than to all eight types keeps irrelevant parameters out
    of the model's reach — G3 rejects unexpected params, so offering fewer of
    them is the difference between a first-attempt pass and a retry.
    """
    param_properties: dict[str, dict] = {}
    for visual in concept.allowed_visuals:
        param_properties.update(VISUAL_PARAM_TYPES[visual])

    return {
        "type": "object",
        "properties": {
            "concept": {"type": "string"},
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "string"},
                        "heading": {"type": "string"},
                        "narration": {"type": "string"},
                        "visual": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": list(concept.allowed_visuals),
                                },
                                "params": {
                                    "type": "object",
                                    "properties": param_properties,
                                },
                            },
                            "required": ["type", "params"],
                        },
                    },
                    "required": ["scene_id", "heading", "narration", "visual"],
                },
            },
        },
        "required": ["concept", "scenes"],
    }


class GeminiProvider:
    """Real generation. The only place `google-genai` is imported."""

    def __init__(self, settings: Settings, prompts: PromptBuilder | None = None):
        self._settings = settings
        self._prompts = prompts or PromptBuilder()
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._settings.gemini_api_key)
        return self._client

    def generate_script(
        self, concept: ConceptContract, feedback: GateFeedback | None = None
    ) -> RawScript:
        from google.genai import types

        response = self._get_client().models.generate_content(
            model=self._settings.gemini_model,
            contents=self._prompts.build(concept, feedback),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema(concept),
                temperature=0.4,
            ),
        )

        usage = getattr(response, "usage_metadata", None)
        return RawScript(
            text=response.text or "",
            prompt_tokens=_count(usage, "prompt_token_count"),
            output_tokens=_count(usage, "candidates_token_count"),
            thinking_tokens=_count(usage, "thoughts_token_count"),
            model=self._settings.gemini_model,
        )


def _count(usage: object, name: str) -> int:
    """Non-thinking responses report `None`, not 0."""
    return int(getattr(usage, name, 0) or 0)


@dataclass
class RecordedLLMProvider:
    """Replays captured responses — no network, no quota, deterministic.

    This is how the gates are proven to work against real broken output
    (SPEC.md §11), and it is what keeps the test suite runnable offline.
    """

    responses: Sequence[str]
    model: str | None = "recorded"
    calls: list[tuple[str, GateFeedback | None]] = field(default_factory=list)

    def generate_script(
        self, concept: ConceptContract, feedback: GateFeedback | None = None
    ) -> RawScript:
        index = len(self.calls)
        if index >= len(self.responses):
            raise IndexError(
                f"RecordedLLMProvider has {len(self.responses)} responses but "
                f"was called {index + 1} times"
            )
        self.calls.append((concept.key, feedback))
        return RawScript(text=self.responses[index], model=self.model)
