"""Two labelled columns of contrasting rows.

The comparison concept's failure mode, named in SPEC.md §6.3, is a script that
explains one bonding type thoroughly and mentions the other in passing. This
layout makes that impossible to hide: every row needs both cells filled, so a
lopsided explanation is visible in the picture before anyone reads it.
"""

from __future__ import annotations

import textwrap
from typing import Any

from matplotlib.axes import Axes

from app.providers.theme import PALETTE

LEFT_X, RIGHT_X = 0.27, 0.73
HEADER_Y = 0.90
CELL_WRAP = 26


def draw(axes: Axes, params: dict[str, Any], ctx) -> None:
    rows = params["rows"]

    axes.set_xlim(0, 1)
    axes.set_ylim(0, 1)

    for x, title, colour in (
        (LEFT_X, params["left_title"], PALETTE.acid),
        (RIGHT_X, params["right_title"], PALETTE.base),
    ):
        axes.text(x, HEADER_Y, title, fontsize=22, fontweight="bold",
                  color=colour, ha="center", va="center")

    axes.plot([0.05, 0.95], [HEADER_Y - 0.10, HEADER_Y - 0.10],
              color=PALETTE.muted, linewidth=1, alpha=0.35)
    axes.plot([0.5, 0.5], [0.02, HEADER_Y - 0.02],
              color=PALETTE.muted, linewidth=1, alpha=0.28)

    # Rows are spaced by count, so 2 rows fill the zone as evenly as 4 do
    # instead of clustering at the top.
    span = HEADER_Y - 0.22
    step = span / len(rows)

    for index, (left_cell, right_cell) in enumerate(rows):
        y = HEADER_Y - 0.20 - step * (index + 0.5)

        if index:
            axes.plot([0.05, 0.95], [y + step / 2, y + step / 2],
                      color=PALETTE.muted, linewidth=0.8, alpha=0.16)

        for x, cell in ((LEFT_X, left_cell), (RIGHT_X, right_cell)):
            lines = textwrap.wrap(cell, width=CELL_WRAP)[:2]
            for offset, line in enumerate(lines):
                axes.text(x, y + 0.035 * (len(lines) - 1) / 2 - offset * 0.035,
                          line, fontsize=17, color=PALETTE.ink,
                          ha="center", va="center")
