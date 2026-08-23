"""Gate tests G1-G4 — the reliability core of this project.

These tests are **pre-written** (PLAN.md §0.1) and are red on purpose until
round 5 implements `app/pipeline/gates.py`. Do not edit them to make them pass;
CLAUDE.md §5 and §11 both forbid it.

They pin down the contract round 5 must implement:

    run_script_gates(raw: str, concept: ConceptContract) -> ScriptGateResult

        .ok       bool
        .script   Script | None      parsed script, present only when ok
        .failure  GateFailure | None named failure, present only when not ok

    GateFailure(gate, reason, detail)
        gate      "G1" | "G2" | "G3" | "G4"
        reason    a stable slug, asserted below
        detail    human-readable, and specific enough to feed prompts/retry.md.j2

Gates **return** failures, they never raise (SPEC.md §10). G1-G4 run in order
and stop at the first failure (SPEC.md §9.1), so a fixture that breaks several
rules is always reported against the earliest gate.

Within G2 the checks run in this order, which is what makes the fixture corpus
unambiguous:

    scene_count -> scene_ids -> narration_empty -> markdown
                -> total_words -> narration_length

Note that with `scene_count` fixed at exactly 5, the per-scene bound (25-38)
and the total bound (125-190) imply each other at the extremes. The total is
the bound that carries the product contract — 125-190 words is roughly 50-76
seconds at normal narration pace, comfortably inside the 45-90s video — so it
is checked first and is the one the corpus exercises.
"""

import json
from pathlib import Path

import pytest

from app.concepts.registry import get_concept
from app.pipeline.gates import run_script_gates

FIXTURES = Path(__file__).parent / "fixtures" / "llm"

VALID = ["valid_ph.json", "valid_covalent.json", "valid_comparison.json"]


def load(name):
    """Return a fixture exactly as an LLM would hand it over: unparsed text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def concept_of(name):
    """Every JSON fixture declares the concept it was generated for."""
    if not name.endswith(".json"):
        return get_concept("ph_scale")
    return get_concept(json.loads(load(name))["concept"])


def run(name):
    return run_script_gates(load(name), concept_of(name))


def assert_failed(name, gate, reason):
    result = run(name)
    assert not result.ok, f"{name} should not pass the gates"
    assert result.failure is not None, f"{name} failed without a GateFailure"
    assert (result.failure.gate, result.failure.reason) == (gate, reason), (
        f"{name} was rejected by {result.failure.gate}/{result.failure.reason}, "
        f"expected {gate}/{reason}: {result.failure.detail}"
    )
    return result.failure


# ---------------------------------------------------------------------------
# The corpus of known-good scripts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", VALID)
def test_hand_written_reference_scripts_pass_every_gate(name):
    result = run(name)
    assert result.ok, (
        f"{name} is a reference script and must pass: "
        f"{result.failure and result.failure.detail}"
    )
    assert result.script is not None
    assert len(result.script.scenes) == 5


@pytest.mark.parametrize("name", VALID)
def test_a_passing_script_reports_no_failure(name):
    assert run(name).failure is None


# ---------------------------------------------------------------------------
# G1 — schema. Structured output is a constraint, not a guarantee.
# ---------------------------------------------------------------------------

def test_g1_rejects_a_response_that_is_not_json_at_all():
    failure = assert_failed("g1_not_json.txt", "G1", "not_json")
    assert failure.detail


def test_g1_rejects_a_scene_missing_a_required_field():
    assert_failed("g1_missing_field.json", "G1", "schema_invalid")


def test_g1_rejects_a_field_of_the_wrong_type():
    assert_failed("g1_wrong_type.json", "G1", "schema_invalid")


def test_g1_names_the_offending_field_so_the_retry_prompt_can_quote_it():
    failure = assert_failed("g1_missing_field.json", "G1", "schema_invalid")
    assert "narration" in failure.detail.lower()


# ---------------------------------------------------------------------------
# G2 — structure. A script can parse cleanly and still break the product.
# ---------------------------------------------------------------------------

def test_g2_rejects_a_script_with_four_scenes():
    assert_failed("g2_four_scenes.json", "G2", "scene_count")


def test_g2_rejects_a_script_with_six_scenes():
    assert_failed("g2_six_scenes.json", "G2", "scene_count")


def test_g2_rejects_a_scene_whose_narration_is_empty():
    assert_failed("g2_empty_narration.json", "G2", "narration_empty")


def test_g2_rejects_script_whose_total_word_count_would_undershoot_forty_five_seconds():
    assert_failed("g2_total_words_low.json", "G2", "total_words")


def test_g2_rejects_script_whose_total_word_count_would_exceed_ninety_seconds():
    assert_failed("g2_total_words_high.json", "G2", "total_words")


def test_g2_failure_detail_states_the_observed_and_expected_budget():
    failure = assert_failed("g2_total_words_high.json", "G2", "total_words")
    assert "340" in failure.detail, (
        "the retry prompt has to tell the model how far over budget it went; "
        f"got: {failure.detail}"
    )


# ---------------------------------------------------------------------------
# G3 — renderer contract. Anything that passes G3 can be rendered.
# ---------------------------------------------------------------------------

def test_g3_rejects_a_visual_type_the_concept_renderer_does_not_know():
    # atom_pair is a real VisualType, but it is not in ph_scale's
    # allowed_visuals, so the ph_scale renderer has never heard of it.
    failure = assert_failed(
        "g3_unknown_visual_type.json", "G3", "visual_type_not_allowed")
    assert "atom_pair" in failure.detail


def test_g3_rejects_a_visual_missing_a_required_param():
    failure = assert_failed("g3_missing_param.json", "G3", "missing_param")
    assert "shared_pairs" in failure.detail


def test_g3_rejects_a_param_outside_its_allowed_range():
    failure = assert_failed(
        "g3_param_out_of_range.json", "G3", "param_out_of_range")
    assert "19.5" in failure.detail or "markers" in failure.detail


# ---------------------------------------------------------------------------
# G4 — concept anchors. The only gate that catches fluent, well-formed,
# perfectly renderable answers to the wrong question.
# ---------------------------------------------------------------------------

def test_g4_rejects_covalent_script_missing_energy_rationale():
    """The single most important test in this file.

    `g4_covalent_no_energy.json` is valid JSON, has exactly five scenes, sits
    inside the word budget, and every visual renders. It explains what a
    covalent bond *is* with complete accuracy — and never once says why one
    forms. Only G4 can tell it apart from the real answer.
    """
    failure = assert_failed(
        "g4_covalent_no_energy.json", "G4", "missing_anchor")
    detail = failure.detail.lower()
    assert any(word in detail for word in ("stable", "stability", "energy")), (
        "the retry prompt must name the missing anchor, not just say 'G4 failed'; "
        f"got: {failure.detail}"
    )


def test_g4_rejects_comparison_that_only_explains_ionic():
    failure = assert_failed(
        "g4_comparison_ionic_only.json", "G4", "missing_anchor")
    assert "covalent" in failure.detail.lower()


def test_g4_rejects_ph_script_that_drops_the_logarithm_beat():
    failure = assert_failed("g4_ph_no_logarithm.json", "G4", "missing_anchor")
    assert "logarithm" in failure.detail.lower()


def test_g4_rejects_a_script_that_drifts_into_a_forbidden_topic():
    failure = assert_failed(
        "g4_cross_contamination.json", "G4", "forbidden_topic")
    detail = failure.detail.lower()
    assert any(t in detail for t in ("ionic", "hybridis", "molecular orbital"))


def test_g4_accepts_the_word_ionic_when_the_concept_is_the_comparison():
    """A forbidden topic for one concept is required vocabulary for another.

    `valid_comparison.json` is dense with the word "ionic", which would be
    drift in a covalent_bonds script. G4 reads forbidden_topics from the
    concept contract, so it must not fire here.
    """
    assert run("valid_comparison.json").ok


# ---------------------------------------------------------------------------
# Cross-cutting behaviour the whole pipeline depends on
# ---------------------------------------------------------------------------

ALL_BROKEN = [
    ("g1_not_json.txt", "G1"),
    ("g1_missing_field.json", "G1"),
    ("g1_wrong_type.json", "G1"),
    ("g2_four_scenes.json", "G2"),
    ("g2_six_scenes.json", "G2"),
    ("g2_empty_narration.json", "G2"),
    ("g2_total_words_low.json", "G2"),
    ("g2_total_words_high.json", "G2"),
    ("g3_unknown_visual_type.json", "G3"),
    ("g3_missing_param.json", "G3"),
    ("g3_param_out_of_range.json", "G3"),
    ("g4_covalent_no_energy.json", "G4"),
    ("g4_comparison_ionic_only.json", "G4"),
    ("g4_ph_no_logarithm.json", "G4"),
    ("g4_cross_contamination.json", "G4"),
]


@pytest.mark.parametrize("name,gate", ALL_BROKEN)
def test_every_broken_fixture_is_classified_to_its_intended_gate(name, gate):
    result = run(name)
    assert not result.ok
    assert result.failure.gate == gate


@pytest.mark.parametrize("name,gate", ALL_BROKEN)
def test_gates_return_a_failure_rather_than_raising(name, gate):
    """SPEC.md §10: gates return GateFailure, they do not raise.

    The runner turns a GateFailure into retry feedback; an exception would
    escape into the task and surface as internal_error instead.
    """
    result = run(name)  # must not raise
    assert result.failure is not None
    assert result.script is None


@pytest.mark.parametrize("name,gate", ALL_BROKEN)
def test_every_failure_carries_detail_usable_as_retry_feedback(name, gate):
    detail = run(name).failure.detail
    assert isinstance(detail, str) and len(detail) > 10, (
        "prompts/retry.md.j2 interpolates this; a bare gate name is not feedback"
    )


def test_gates_stop_at_the_first_failure():
    """`g1_missing_field.json` breaks G1 and would also break G2.

    It is one scene short of the word budget because the field was removed.
    G1 runs first, so that is what must be reported — otherwise the retry
    feedback would send the model chasing the wrong problem.
    """
    assert_failed("g1_missing_field.json", "G1", "schema_invalid")
