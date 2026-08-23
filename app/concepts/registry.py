"""Concept contracts — SPEC.md §6. Data, not logic.

Adding a fourth STEM topic is one entry here plus any new visual type. No
pipeline change, no prompt change. That is how extensibility is demonstrated
(CLAUDE.md §2) rather than by implementing extra topics.

Anchors are the G4 vocabulary. Each is a named requirement satisfied by any
one of its terms; `min_scenes` raises the bar from "mentioned" to "actually
carried through the script", which is what distinguishes a comparison from
two mini-lectures glued together.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.job import ConceptKey
from app.domain.script import VisualType

FALLBACK_DIR = Path(__file__).parent / "fallbacks"


class Beat(BaseModel):
    """A required content point, in order. Rendered into the prompt."""

    id: str
    content: str


class Anchor(BaseModel):
    """A G4 requirement. Satisfied when any term appears in the narration.

    `name` is what the retry prompt quotes back at the model, so it reads as
    a thing to fix rather than as a gate identifier.
    """

    name: str
    any_of: list[str]
    min_scenes: int = 1


class ConceptContract(BaseModel):
    key: ConceptKey
    canonical_question: str

    title: str
    """Short display name for the title card (SPEC.md §7).

    Distinct from `canonical_question` on purpose: the title card already shows
    the learner's own wording, so repeating a question beside it would read as
    a bug. This is the one line the learner sees that names the topic itself."""

    aliases: list[tuple[str, ...]]
    """Resolver phrases. Every term in a tuple must be present for it to match,
    which is what lets one entry mean "mentions both sides"."""

    excludes_if_present: list[str] = Field(default_factory=list)
    """Terms that disqualify this concept even when an alias matched.

    Without this, "difference between ionic and covalent bonding" matches
    `covalent_bonds` too, and the clearest question in scope comes back as
    `ambiguous_query`."""

    narrative_shape: Literal["linear", "causal", "comparative"]
    beats: list[Beat]
    anchors: list[Anchor]
    allowed_visuals: list[VisualType]
    required_visuals: list[VisualType] = Field(default_factory=list)
    forbidden_topics: list[str]
    fallback_path: Path


PH_SCALE = ConceptContract(
    key="ph_scale",
    title="The pH scale",
    canonical_question="How does the pH scale work?",
    aliases=[("ph scale",), ("ph",), ("hydrogen ion",), ("acidity",),
             ("acids and bases",)],
    narrative_shape="linear",
    beats=[
        Beat(id="B1", content="pH measures H+ (hydronium) concentration in solution"),
        Beat(id="B2", content="Scale runs 0-14; 7 is neutral"),
        Beat(id="B3", content="Below 7 acidic, above 7 basic or alkaline"),
        Beat(id="B4", content="Logarithmic - each step is a 10x change in H+"),
        Beat(id="B5", content="Real anchors: lemon ~2, water 7, bleach ~13"),
    ],
    anchors=[
        Anchor(name="hydrogen ions", any_of=["h+", "hydrogen ion"]),
        # Both forms: a live run wrote "seven" in words, which establishes
        # the beat perfectly well. Rejecting it would be a false negative
        # that costs a whole extra LLM call to correct.
        Anchor(name="the neutral value 7", any_of=["7", "seven"]),
        Anchor(name="neutral", any_of=["neutral"]),
        Anchor(name="the acidic side", any_of=["acidic", "acid"]),
        Anchor(name="the basic side", any_of=["basic", "alkaline"]),
        # B4. The only beat that answers *how it works*; without it the video
        # is "small = sour, big = slippery" - true but empty (SPEC.md §6.1).
        Anchor(name="the logarithmic step",
               any_of=["logarithmic", "logarithm", "10 times", "ten times",
                       "tenfold", "ten-fold", "hundredfold"]),
    ],
    allowed_visuals=["title_card", "ph_scale_bar", "log_steps", "summary_card"],
    forbidden_topics=["titration", "buffer", "pka"],
    fallback_path=FALLBACK_DIR / "ph_scale.json",
)

COVALENT_BONDS = ConceptContract(
    key="covalent_bonds",
    title="Covalent bonding",
    canonical_question="Why do atoms form covalent bonds?",
    aliases=[("covalent bond",), ("covalent bonds",), ("covalent bonding",),
             ("share electrons",), ("sharing electrons",), ("shared electrons",),
             ("electron sharing",)],
    excludes_if_present=["ionic", "difference", "differ", "different", "compare",
                         "compared", "comparison", "versus", "vs"],
    narrative_shape="causal",
    beats=[
        Beat(id="B1", content="Atoms share electrons (not give or take)"),
        Beat(id="B2", content="Motive: a stable outer shell (octet; duet for H)"),
        Beat(id="B3", content="Energy rationale: the bonded state is lower in "
                              "energy than two separate atoms"),
        Beat(id="B4", content="Force balance: nuclei-shared-pair attraction vs "
                              "nucleus-nucleus repulsion"),
        Beat(id="B5", content="Example: H2, and H2O or CH4"),
    ],
    anchors=[
        Anchor(name="electron sharing", any_of=["share", "shares", "shared", "sharing"]),
        Anchor(name="the outer shell", any_of=["outer shell", "valence", "octet"]),
        # B3. The single most important anchor in the project: without it the
        # model describes what a covalent bond *is* instead of *why it forms*,
        # and the JSON is flawless while answering a different question.
        Anchor(name="the energy rationale for stability",
               any_of=["stable", "stability", "lower energy", "lower in energy"]),
    ],
    allowed_visuals=["title_card", "atom_pair", "energy_curve", "summary_card"],
    forbidden_topics=["ionic bonding", "hybridis", "molecular orbital"],
    fallback_path=FALLBACK_DIR / "covalent_bonds.json",
)

IONIC_VS_COVALENT = ConceptContract(
    key="ionic_vs_covalent",
    title="Ionic vs covalent bonding",
    canonical_question="What is the difference between ionic and covalent bonding?",
    aliases=[("ionic", "covalent"), ("ionic vs covalent",),
             ("difference between ionic and covalent",)],
    narrative_shape="comparative",
    beats=[
        Beat(id="B1", content="Mechanism: ionic transfers, covalent shares"),
        Beat(id="B2", content="Participants: metal + non-metal (large "
                              "electronegativity gap) vs non-metal + non-metal"),
        Beat(id="B3", content="Product: ions in a lattice vs discrete molecules"),
        Beat(id="B4", content="Properties: high melting point and conducts when "
                              "molten or dissolved vs low melting point, usually not"),
        Beat(id="B5", content="Paired examples: NaCl vs H2O or CH4"),
    ],
    anchors=[
        # Both sides in at least two scenes each. One passing mention of the
        # other type is what "two mini-lectures glued together" looks like.
        Anchor(name="ionic bonding", any_of=["ionic"], min_scenes=2),
        Anchor(name="covalent bonding", any_of=["covalent"], min_scenes=2),
        Anchor(name="electron transfer",
               any_of=["transfer", "transfers", "transferred", "transferring"]),
        Anchor(name="electron sharing", any_of=["share", "shares", "shared", "sharing"]),
        Anchor(name="a worked example", any_of=["nacl", "sodium chloride"]),
        Anchor(name="a contrasting property",
               any_of=["melting point", "melting points", "conduct", "conducts",
                       "conducting"]),
    ],
    allowed_visuals=["title_card", "electron_transfer", "side_by_side_comparison",
                     "summary_card"],
    # SPEC.md §6.3: the comparative shape is enforced at the visual layer, not
    # only in prose. Prose can fake a comparison; a required visual cannot.
    required_visuals=["side_by_side_comparison"],
    forbidden_topics=["hybridis", "molecular orbital", "titration"],
    fallback_path=FALLBACK_DIR / "ionic_vs_covalent.json",
)

_REGISTRY: dict[str, ConceptContract] = {
    c.key: c for c in (PH_SCALE, COVALENT_BONDS, IONIC_VS_COVALENT)
}


def get_concept(key: str) -> ConceptContract:
    """Raises `KeyError` for an unknown key — callers resolve first."""
    return _REGISTRY[key]


def all_concepts() -> list[ConceptContract]:
    return list(_REGISTRY.values())


def supported_keys() -> list[str]:
    return list(_REGISTRY)
