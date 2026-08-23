"""Opening frame — SPEC.md §13.

The only scene type whose content the model does not supply. It shows the
learner's **original query, verbatim**, which is the visual proof R6 asks for:
the finished video can be held next to the question that produced it.

That the query appears here and nowhere in the prompt is the whole point. It is
displayed as data, never interpreted as instruction (CLAUDE.md §4).
"""

from __future__ import annotations

import textwrap
from typing import Any

from matplotlib.axes import Axes

from app.providers.theme import PALETTE


def draw(axes: Axes, params: dict[str, Any], ctx) -> None:
    axes.set_xlim(0, 1)
    axes.set_ylim(0, 1)

    axes.text(
        0.5, 0.86, ctx.concept_title,
        fontsize=40, fontweight="bold", color=PALETTE.ink,
        ha="center", va="center",
    )

    axes.plot([0.38, 0.62], [0.68, 0.68], color=PALETTE.base,
              linewidth=3, solid_capstyle="round")

    axes.text(
        0.5, 0.48, "you asked",
        fontsize=13, color=PALETTE.muted, ha="center", va="center",
        style="italic",
    )

    # Verbatim, wrapped only for width. Never re-phrased, never truncated to a
    # concept name - a learner must recognise their own words.
    for index, line in enumerate(textwrap.wrap(ctx.query.strip(), width=52)):
        axes.text(
            0.5, 0.26 - index * 0.15, line,
            fontsize=22, color=PALETTE.neutral_hue,
            ha="center", va="center",
        )
