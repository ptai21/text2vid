"""Concept registry and rule-based resolver — SPEC.md §6.

Deterministic, no LLM, no network, no cost (CLAUDE.md §4). A wrong guess at
the front door is worse than a clear rejection, so everything here is either
an exact rule or an explicit refusal.
"""

import pytest

from app.concepts.aliases import ResolutionError, normalise, resolve
from app.concepts.registry import all_concepts, get_concept

CANONICAL = {
    "How does the pH scale work?": "ph_scale",
    "Why do atoms form covalent bonds?": "covalent_bonds",
    "What is the difference between ionic and covalent bonding?": "ionic_vs_covalent",
}


def code_of(query):
    with pytest.raises(ResolutionError) as excinfo:
        resolve(query)
    return excinfo.value.code


# ---------------------------------------------------------------------------
# The registry is data
# ---------------------------------------------------------------------------

def test_the_registry_holds_exactly_the_three_supported_concepts():
    assert sorted(c.key for c in all_concepts()) == [
        "covalent_bonds", "ionic_vs_covalent", "ph_scale",
    ]


@pytest.mark.parametrize("question,key", CANONICAL.items())
def test_each_contract_states_its_canonical_question(question, key):
    assert get_concept(key).canonical_question == question


def test_asking_for_an_unknown_concept_key_raises():
    with pytest.raises(KeyError):
        get_concept("thermodynamics")


@pytest.mark.parametrize("key,shape", [
    ("ph_scale", "linear"),
    ("covalent_bonds", "causal"),
    ("ionic_vs_covalent", "comparative"),
])
def test_narrative_shape_matches_the_spec(key, shape):
    assert get_concept(key).narrative_shape == shape


@pytest.mark.parametrize("key,visuals", [
    ("ph_scale", {"title_card", "ph_scale_bar", "log_steps", "summary_card"}),
    ("covalent_bonds", {"title_card", "atom_pair", "energy_curve", "summary_card"}),
    ("ionic_vs_covalent",
     {"title_card", "electron_transfer", "side_by_side_comparison", "summary_card"}),
])
def test_allowed_visuals_match_the_spec(key, visuals):
    assert set(get_concept(key).allowed_visuals) == visuals


def test_every_concept_carries_beats_anchors_and_drift_guards():
    for concept in all_concepts():
        assert len(concept.beats) == 5, f"{concept.key} should have five beats"
        assert concept.anchors, f"{concept.key} has no G4 anchors"
        assert concept.forbidden_topics, f"{concept.key} has no drift guards"


def test_the_comparison_concept_requires_a_side_by_side_visual():
    """SPEC.md §6.3: the comparative shape is enforced at the visual layer.

    Prose alone can fake a comparison; a required visual cannot be faked.
    """
    assert "side_by_side_comparison" in get_concept("ionic_vs_covalent").required_visuals


def test_every_concept_points_at_a_fallback_script():
    for concept in all_concepts():
        assert concept.fallback_path.name.endswith(".json")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def test_normalisation_lowercases_strips_punctuation_and_collapses_whitespace():
    assert normalise("  How   does the pH SCALE work??  ") == "how does the ph scale work"


# ---------------------------------------------------------------------------
# Resolution — the happy path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,key", CANONICAL.items())
def test_the_canonical_questions_resolve(question, key):
    assert resolve(question) == key


@pytest.mark.parametrize("query,key", [
    ("what is ph", "ph_scale"),
    ("Explain the pH scale.", "ph_scale"),
    ("how is pH measured?", "ph_scale"),
    ("why do atoms share electrons", "covalent_bonds"),
    ("how do covalent bonds form?", "covalent_bonds"),
    ("what is a covalent bond", "covalent_bonds"),
    ("ionic vs covalent", "ionic_vs_covalent"),
    ("compare ionic and covalent bonds", "ionic_vs_covalent"),
    ("how are ionic and covalent bonding different?", "ionic_vs_covalent"),
])
def test_paraphrases_resolve(query, key):
    assert resolve(query) == key


def test_resolution_ignores_case_and_punctuation():
    assert resolve("HOW DOES THE PH SCALE WORK???") == "ph_scale"


# ---------------------------------------------------------------------------
# The disambiguation that actually bites
# ---------------------------------------------------------------------------

def test_the_comparison_question_does_not_also_match_covalent_bonds():
    """The trap: "difference between ionic and covalent bonding" literally
    contains "covalent bonding".

    A naive alias match returns two concepts and the learner gets
    `ambiguous_query` for the single most clearly-worded question in scope.
    Mentioning ionic bonding disqualifies the covalent-only concept.
    """
    assert resolve("What is the difference between ionic and covalent bonding?") \
        == "ionic_vs_covalent"


@pytest.mark.parametrize("query", [
    "ionic versus covalent bonds",
    "how does ionic bonding differ from covalent bonding",
    "covalent and ionic bonding compared",
])
def test_comparison_phrasings_never_fall_through_to_covalent_bonds(query):
    assert resolve(query) == "ionic_vs_covalent"


# ---------------------------------------------------------------------------
# Rejections — SPEC.md §6.4
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "What is photosynthesis?",
    "How do I balance a redox equation?",
    "Explain thermodynamics",
    "What is the capital of France?",
])
def test_out_of_scope_questions_are_rejected_as_unsupported(query):
    assert code_of(query) == "unsupported_concept"


def test_an_unsupported_rejection_lists_the_supported_concepts():
    """SPEC.md §5: the error envelope carries `supported_concepts`."""
    with pytest.raises(ResolutionError) as excinfo:
        resolve("What is photosynthesis?")
    assert set(excinfo.value.supported_concepts) == {
        "ph_scale", "covalent_bonds", "ionic_vs_covalent",
    }


def test_a_query_matching_two_concepts_is_ambiguous():
    assert code_of("how does ph relate to ionic and covalent bonding") \
        == "ambiguous_query"


@pytest.mark.parametrize("query", ["", "  ", "ph"])
def test_a_query_shorter_than_three_characters_is_invalid(query):
    assert code_of(query) == "invalid_request"


def test_a_query_longer_than_five_hundred_characters_is_invalid():
    assert code_of("How does the pH scale work? " + "x" * 500) == "invalid_request"


def test_length_is_checked_before_concept_matching():
    """A 600-character essay about the pH scale is still a malformed request.

    Reporting `unsupported_concept` there would send the client looking for a
    concept problem that does not exist.
    """
    assert code_of("how does the ph scale work " * 40) == "invalid_request"


def test_resolution_never_costs_anything():
    """CLAUDE.md §4: rule-based, no LLM router.

    Guarded by construction — `app/concepts/` may not import providers.
    """
    import app.concepts.aliases as module

    source = module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "providers" not in text and "genai" not in text
