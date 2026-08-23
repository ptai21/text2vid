"""Script validation gates G1-G4 — SPEC.md §10.

LLM output is untrusted input. These four gates are the whole reliability
story for the scripting stage, and each earns its place (R8):

- **G1** structured output is a constraint, not a guarantee. Cheapest check.
- **G2** a 2-scene or 11-scene script breaks the product even when it parses.
- **G3** the load-bearing *technical* gate: anything that passes can be
  rendered, so the renderer never meets a visual type it has never heard of.
- **G4** the load-bearing *product* gate: the only check that catches
  perfectly valid JSON with off-target content.

Gates **return** `GateFailure`; they never raise. The runner turns a failure
into retry feedback, and an exception would instead escape into the task and
surface as `internal_error` — a bug report where a retry belongs.

They run in order and stop at the first failure, so a script that breaks
several rules is reported against the earliest one. Sending retry feedback
about anchors when the JSON did not even parse would chase the wrong problem.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from app.concepts.registry import Anchor, ConceptContract
from app.domain.script import Script

# SPEC.md §7. Exactly five scenes at 25-38 words is 125-190 words total, which
# is roughly 50-76 seconds of narration - comfortably inside the 45-90s video.
SCENE_COUNT = 5
MIN_SCENE_WORDS = 25
MAX_SCENE_WORDS = 38
MIN_TOTAL_WORDS = 125
MAX_TOTAL_WORDS = 190

MARKDOWN_CHARS = "*_#`|~"
BULLET_PREFIXES = ("- ", "* ", "• ", "· ")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)(?:```|\Z)", re.DOTALL)


@dataclass(frozen=True)
class GateFailure:
    gate: str
    reason: str
    detail: str
    """Specific enough to interpolate into prompts/retry.md.j2.

    A retry without feedback is a second lottery ticket; a retry that names
    the missing anchor is engineering (SPEC.md §8)."""


@dataclass(frozen=True)
class ScriptGateResult:
    ok: bool
    script: Script | None = None
    failure: GateFailure | None = None


# ---------------------------------------------------------------------------
# G3 param contracts — SPEC.md §7 VisualType table
# ---------------------------------------------------------------------------

Check = Callable[[Any], "tuple[str, str] | None"]


def _number(minimum: float | None = None, maximum: float | None = None) -> Check:
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "param_type", f"expected a number, got {type(value).__name__}"
        if minimum is not None and value < minimum:
            return "param_out_of_range", f"value {value} is below {minimum}"
        if maximum is not None and value > maximum:
            return "param_out_of_range", f"value {value} is above {maximum}"
        return None

    return check


def _integer(minimum: int | None = None, maximum: int | None = None) -> Check:
    def check(value):
        if isinstance(value, bool) or not isinstance(value, int):
            return "param_type", f"expected an integer, got {type(value).__name__}"
        if minimum is not None and value < minimum:
            return "param_out_of_range", f"value {value} is below {minimum}"
        if maximum is not None and value > maximum:
            return "param_out_of_range", f"value {value} is above {maximum}"
        return None

    return check


def _text() -> Check:
    def check(value):
        if not isinstance(value, str) or not value.strip():
            return "param_type", "expected a non-empty string"
        return None

    return check


def _number_list(max_items: int, minimum: float, maximum: float) -> Check:
    item = _number(minimum, maximum)

    def check(value):
        if not isinstance(value, list) or not value:
            return "param_type", "expected a non-empty list of numbers"
        if len(value) > max_items:
            return "param_out_of_range", f"{len(value)} items, at most {max_items}"
        for index, element in enumerate(value):
            problem = item(element)
            if problem is not None:
                reason, message = problem
                return reason, f"{message} at index {index}"
        return None

    return check


def _text_list(min_items: int, max_items: int, max_words: int) -> Check:
    def check(value):
        if not isinstance(value, list):
            return "param_type", "expected a list of strings"
        if not min_items <= len(value) <= max_items:
            return ("param_out_of_range",
                    f"{len(value)} items, expected {min_items}-{max_items}")
        for index, element in enumerate(value):
            if not isinstance(element, str) or not element.strip():
                return "param_type", f"item {index} is not a non-empty string"
            if len(element.split()) > max_words:
                return ("param_out_of_range",
                        f"item {index} has {len(element.split())} words, "
                        f"at most {max_words}")
        return None

    return check


def _pair_rows(min_items: int, max_items: int) -> Check:
    def check(value):
        if not isinstance(value, list):
            return "param_type", "expected a list of [left, right] pairs"
        if not min_items <= len(value) <= max_items:
            return ("param_out_of_range",
                    f"{len(value)} rows, expected {min_items}-{max_items}")
        for index, row in enumerate(value):
            if (not isinstance(row, (list, tuple)) or len(row) != 2
                    or not all(isinstance(cell, str) and cell.strip() for cell in row)):
                return "param_type", f"row {index} is not a pair of non-empty strings"
        return None

    return check


VISUAL_PARAMS: dict[str, dict[str, Check]] = {
    "title_card": {},
    "ph_scale_bar": {"markers": _number_list(max_items=4, minimum=0.0, maximum=14.0)},
    "log_steps": {"from_ph": _integer(0, 14), "to_ph": _integer(0, 14)},
    "atom_pair": {
        "left": _text(), "right": _text(), "shared_pairs": _integer(1, 3),
    },
    "energy_curve": {"min_distance": _number(), "label": _text()},
    "electron_transfer": {"donor": _text(), "acceptor": _text()},
    "side_by_side_comparison": {
        "left_title": _text(), "right_title": _text(), "rows": _pair_rows(2, 4),
    },
    "summary_card": {"points": _text_list(2, 4, max_words=10)},
}


# ---------------------------------------------------------------------------
# G1 — schema
# ---------------------------------------------------------------------------

def _strip_fence(raw: str) -> str:
    """Models wrap JSON in a code fence even when told not to.

    Tolerating the fence is not weakening the gate: what follows still has to
    parse and validate, so a truncated or prose response fails either way.
    """
    if "```" not in raw:
        return raw
    match = _FENCE.search(raw)
    return match.group(1) if match else raw


def check_g1(raw: str) -> GateFailure | Script:
    try:
        payload = json.loads(_strip_fence(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        return GateFailure(
            "G1", "not_json",
            f"Response is not valid JSON: {exc}. Return a single JSON object "
            "and nothing else.",
        )

    try:
        return Script.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        return GateFailure("G1", "schema_invalid",
                           f"JSON does not match the script schema: {problems}")


# ---------------------------------------------------------------------------
# G2 — structure
# ---------------------------------------------------------------------------

def check_g2(script: Script) -> GateFailure | None:
    """Checks run in a fixed order so a script breaking several rules is
    always reported against the same one."""
    scenes = script.scenes

    if len(scenes) != SCENE_COUNT:
        return GateFailure(
            "G2", "scene_count",
            f"Script has {len(scenes)} scenes; exactly {SCENE_COUNT} are required.",
        )

    expected_ids = [f"s{index}" for index in range(1, SCENE_COUNT + 1)]
    actual_ids = [scene.scene_id for scene in scenes]
    if actual_ids != expected_ids:
        return GateFailure(
            "G2", "scene_ids",
            f"Scene ids are {actual_ids}; they must be exactly {expected_ids} in order.",
        )

    for scene in scenes:
        if not scene.narration.strip():
            return GateFailure(
                "G2", "narration_empty",
                f"Scene {scene.scene_id} has empty narration; every scene must be "
                f"spoken, {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} words.",
            )

    for scene in scenes:
        found = [char for char in MARKDOWN_CHARS if char in scene.narration]
        if found or scene.narration.lstrip().startswith(BULLET_PREFIXES):
            return GateFailure(
                "G2", "markdown",
                f"Scene {scene.scene_id} narration contains markup ({found}); "
                "narration is read aloud, so it must be plain prose.",
            )

    total = script.total_words
    if not MIN_TOTAL_WORDS <= total <= MAX_TOTAL_WORDS:
        return GateFailure(
            "G2", "total_words",
            f"Narration totals {total} words; the budget is "
            f"{MIN_TOTAL_WORDS}-{MAX_TOTAL_WORDS} words, which is what keeps the "
            "finished video inside 45-90 seconds.",
        )

    for scene in scenes:
        words = len(scene.narration.split())
        if not MIN_SCENE_WORDS <= words <= MAX_SCENE_WORDS:
            return GateFailure(
                "G2", "narration_length",
                f"Scene {scene.scene_id} has {words} words; each scene must be "
                f"{MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} words.",
            )

    return None


# ---------------------------------------------------------------------------
# G3 — renderer contract
# ---------------------------------------------------------------------------

def check_g3(script: Script, concept: ConceptContract) -> GateFailure | None:
    allowed = set(concept.allowed_visuals)

    for scene in script.scenes:
        visual = scene.visual

        if visual.type not in allowed:
            return GateFailure(
                "G3", "visual_type_not_allowed",
                f"Scene {scene.scene_id} uses visual type '{visual.type}', which is "
                f"not available for {concept.key}. Choose one of "
                f"{sorted(allowed)}.",
            )

        schema = VISUAL_PARAMS[visual.type]

        for name, check in schema.items():
            if name not in visual.params:
                return GateFailure(
                    "G3", "missing_param",
                    f"Scene {scene.scene_id} visual '{visual.type}' is missing the "
                    f"required param '{name}'.",
                )
            problem = check(visual.params[name])
            if problem is not None:
                reason, message = problem
                return GateFailure(
                    "G3", reason,
                    f"Scene {scene.scene_id} visual '{visual.type}' param "
                    f"'{name}': {message}.",
                )

        unexpected = sorted(set(visual.params) - set(schema))
        if unexpected:
            return GateFailure(
                "G3", "unexpected_param",
                f"Scene {scene.scene_id} visual '{visual.type}' has unknown params "
                f"{unexpected}; allowed params are {sorted(schema)}.",
            )

    return None


# ---------------------------------------------------------------------------
# G4 — concept anchors
# ---------------------------------------------------------------------------

def _contains(haystack: str, phrase: str) -> bool:
    """Anchored at the start, open at the end.

    The leading `(?<!\\w)` stops "ion" matching inside "champion"; the trailing
    `\\w*` lets "hydrogen ion" match "hydrogen ions" and "share" match
    "shared". Requiring exact inflections instead would mean enumerating every
    plural and tense in the registry, and missing one produces a false
    rejection — a retry that tells the model to add something it already said.
    """
    return re.search(rf"(?<!\w){re.escape(phrase)}\w*", haystack) is not None


def _scenes_satisfying(script: Script, anchor: Anchor) -> int:
    return sum(
        1
        for scene in script.scenes
        if any(_contains(scene.narration.lower(), term) for term in anchor.any_of)
    )


def check_g4(script: Script, concept: ConceptContract) -> GateFailure | None:
    """The only gate that can tell a correct answer from a fluent wrong one."""
    for anchor in concept.anchors:
        found = _scenes_satisfying(script, anchor)
        if found < anchor.min_scenes:
            where = (
                f"in at least {anchor.min_scenes} separate scenes"
                if anchor.min_scenes > 1
                else "anywhere in the narration"
            )
            return GateFailure(
                "G4", "missing_anchor",
                f"The script never establishes {anchor.name} {where} "
                f"(found in {found}). Say it explicitly using one of "
                f"{anchor.any_of}.",
            )

    used = {scene.visual.type for scene in script.scenes}
    for required in concept.required_visuals:
        if required not in used:
            return GateFailure(
                "G4", "missing_required_visual",
                f"At least one scene must use the '{required}' visual for "
                f"{concept.key}; the script used {sorted(used)}.",
            )

    corpus = " ".join(scene.narration.lower() for scene in script.scenes)
    for topic in concept.forbidden_topics:
        if topic in corpus:
            return GateFailure(
                "G4", "forbidden_topic",
                f"The script drifts into '{topic}', which is out of scope for "
                f"{concept.key}. Stay on the listed beats.",
            )

    return None


# ---------------------------------------------------------------------------
# The pipeline entry point
# ---------------------------------------------------------------------------

def run_script_gates(raw: str, concept: ConceptContract) -> ScriptGateResult:
    """Run G1 through G4 in order, stopping at the first failure."""
    parsed = check_g1(raw)
    if isinstance(parsed, GateFailure):
        return ScriptGateResult(ok=False, failure=parsed)

    for failure in (
        check_g2(parsed),
        check_g3(parsed, concept),
        check_g4(parsed, concept),
    ):
        if failure is not None:
            return ScriptGateResult(ok=False, failure=failure)

    return ScriptGateResult(ok=True, script=parsed)
