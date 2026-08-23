"""The script schema — SPEC.md §7. The LLM's only output.

This is the boundary where untrusted model output becomes a typed object, so
G1 is exactly "does this parse". Everything the schema can express is
renderable in principle; G3 then checks it is renderable *for this concept*.

The `VisualType` enum is closed on purpose (SPEC.md §16): a new visual needs
code, not just a prompt change. That is the cost of making G3 possible, and it
is the right trade — an open enum means the renderer can meet a type it has
never heard of, at which point validation cannot help.

`params` stays `dict[str, Any]` here rather than a per-type union. Parsing and
renderer-contract checking are deliberately separate gates: collapsing them
would report a param error as a schema error and send the retry prompt after
the wrong problem.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain.job import ConceptKey

VisualType = Literal[
    "title_card",
    "ph_scale_bar",
    "log_steps",
    "atom_pair",
    "energy_curve",
    "electron_transfer",
    "side_by_side_comparison",
    "summary_card",
]


class VisualSpec(BaseModel):
    type: VisualType
    params: dict[str, Any] = Field(default_factory=dict)


class Scene(BaseModel):
    scene_id: str
    heading: str
    narration: str
    visual: VisualSpec


class Script(BaseModel):
    """The model does **not** set scene duration.

    Duration is measured from the synthesised audio (SPEC.md §9.2), which is
    what makes audio/visual sync a consequence rather than a calibration task.
    """

    concept: ConceptKey
    scenes: list[Scene]

    @property
    def total_words(self) -> int:
        return sum(len(scene.narration.split()) for scene in self.scenes)
