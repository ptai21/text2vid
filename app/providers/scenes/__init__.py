"""One drawing module per `VisualType` — SPEC.md §7.

The dispatch table at the bottom is the renderer's half of the contract that
G3 enforces on the model's half. G3 guarantees every `visual.type` reaching
this package is a key in `SCENES` and that its params are present, correctly
typed and in range. That is what lets each `draw` below read `params["..."]`
directly instead of defensively: the validation already happened, at the only
layer that can send useful feedback back to the model.

Keeping the check and the drawing in separate packages is deliberate. If a
ninth visual type is added, `VISUAL_PARAMS` in `pipeline/gates.py` and a module
here must both change - and the test suite fails loudly if only one of them does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from matplotlib.axes import Axes

from app.providers.scenes import (
    atom_pair,
    electron_transfer,
    energy_curve,
    log_steps,
    ph_scale_bar,
    side_by_side_comparison,
    summary_card,
    title_card,
)


@dataclass(frozen=True)
class RenderContext:
    """What the renderer injects that the model never supplies.

    The learner's raw query lives here rather than in the script because it
    never enters the prompt (CLAUDE.md §4). It reaches the screen through this
    struct alone, which is what makes "the query is shown, never obeyed" a
    structural property instead of a promise.
    """

    query: str
    concept_title: str


Draw = Callable[[Axes, dict[str, Any], RenderContext], None]

SCENES: dict[str, Draw] = {
    "title_card": title_card.draw,
    "ph_scale_bar": ph_scale_bar.draw,
    "log_steps": log_steps.draw,
    "atom_pair": atom_pair.draw,
    "energy_curve": energy_curve.draw,
    "electron_transfer": electron_transfer.draw,
    "side_by_side_comparison": side_by_side_comparison.draw,
    "summary_card": summary_card.draw,
}
