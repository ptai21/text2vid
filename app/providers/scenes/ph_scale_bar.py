"""The 0-14 scale with the script's markers pinned on it.

Colour carries the meaning: acid orange at 0, neutral green at 7, base blue at
14, the same three accents used everywhere else. A learner who sees orange in
this scene and orange in the summary card is being told they are the same idea.

The vertical order is fixed and non-overlapping - band labels, then markers,
then the bar, then the ticks - because markers are script-supplied and may land
anywhere, including on top of 0 or 14.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap

from app.providers.theme import PALETTE

SCALE_CMAP = LinearSegmentedColormap.from_list(
    "ph", [PALETTE.acid, PALETTE.neutral_hue, PALETTE.base]
)

BAND_LABEL_Y = 0.94
MARKER_LABEL_Y = 0.80
MARKER_TIP_Y = 0.70
BAR_TOP = 0.64
BAR_BOTTOM = 0.38
TICK_Y = 0.26


def draw(axes: Axes, params: dict[str, Any], ctx) -> None:
    markers = params["markers"]

    # Padding on both sides so a marker sitting exactly on 0 or 14 is drawn
    # whole rather than sliced in half by the axes edge.
    axes.set_xlim(-0.7, 14.7)
    axes.set_ylim(0, 1)

    axes.imshow(
        np.linspace(0, 1, 512).reshape(1, -1),
        extent=(0, 14, BAR_BOTTOM, BAR_TOP),
        aspect="auto", cmap=SCALE_CMAP, interpolation="bilinear", zorder=1,
    )

    for label, position, colour in (
        ("acidic", 3.5, PALETTE.acid),
        ("neutral", 7.0, PALETTE.neutral_hue),
        ("basic", 10.5, PALETTE.base),
    ):
        axes.text(position, BAND_LABEL_Y, label, fontsize=16, color=colour,
                  ha="center", va="center", fontweight="bold")

    for value in range(0, 15):
        emphasis = value in (0, 7, 14)
        axes.text(
            value, TICK_Y, str(value),
            fontsize=16 if emphasis else 12,
            color=PALETTE.ink if emphasis else PALETTE.muted,
            fontweight="bold" if emphasis else "normal",
            ha="center", va="center",
        )

    for value in markers:
        axes.plot([value], [MARKER_TIP_Y], marker="v", markersize=15,
                  color=PALETTE.ink, zorder=4)
        axes.text(value, MARKER_LABEL_Y, f"{value:g}", fontsize=15,
                  fontweight="bold", color=PALETTE.ink,
                  ha="center", va="center", zorder=4)
