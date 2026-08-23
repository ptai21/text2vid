"""The closing recap: 2-4 numbered takeaways.

The last thing on screen is the last thing remembered, so this scene is
deliberately the plainest one in the set. Numbers rather than bullets, because
the schema caps each point at ten words and a numbered list reads as "these are
the things", which is the note an explainer should end on.
"""

from __future__ import annotations

import textwrap
from typing import Any

from matplotlib.axes import Axes

from app.providers.theme import ACCENTS, PALETTE

POINT_WRAP = 46


def draw(axes: Axes, params: dict[str, Any], ctx) -> None:
    points = params["points"]

    axes.set_xlim(0, 1)
    axes.set_ylim(0, 1)

    span = 0.92
    step = span / len(points)

    for index, point in enumerate(points):
        y = 0.94 - step * (index + 0.5)
        colour = ACCENTS[index % len(ACCENTS)]

        axes.scatter([0.09], [y], s=1100, color=colour, alpha=0.20, zorder=1)
        axes.text(0.09, y, str(index + 1), fontsize=21, fontweight="bold",
                  color=colour, ha="center", va="center", zorder=2)

        lines = textwrap.wrap(point, width=POINT_WRAP)[:2]
        for offset, line in enumerate(lines):
            axes.text(0.17, y + 0.038 * (len(lines) - 1) / 2 - offset * 0.038,
                      line, fontsize=19, color=PALETTE.ink,
                      ha="left", va="center")
