"""Two atoms sharing electron pairs — the covalent bond made visible.

The shells genuinely overlap, and the shared electrons are drawn in the lens
where they do. That is the whole claim of a covalent bond stated as geometry:
those electrons are inside *both* shells at once. Each atom keeps its own
accent colour; the shared pair takes the neutral one, because it belongs to
neither and to both.

Contrast `electron_transfer`, which reuses this exact layout with an arrow
crossing the gap instead of dots resting in it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from matplotlib.axes import Axes

from app.providers.theme import PALETTE, circle

RADIUS = 0.13
LEFT_X, RIGHT_X = 0.385, 0.615
CENTRE_Y = 0.50


def draw(axes: Axes, params: dict[str, Any], ctx) -> None:
    left, right = params["left"], params["right"]
    pairs = params["shared_pairs"]

    axes.set_xlim(0, 1)
    axes.set_ylim(0, 1)

    for x, label, colour, label_x in (
        (LEFT_X, left, PALETTE.acid, LEFT_X - 0.068),
        (RIGHT_X, right, PALETTE.base, RIGHT_X + 0.068),
    ):
        axes.add_patch(circle((x, CENTRE_Y), RADIUS, facecolor=colour,
                              alpha=0.16, edgecolor=colour, linewidth=2.5,
                              zorder=1))
        axes.text(label_x, CENTRE_Y, label, fontsize=30, fontweight="bold",
                  color=PALETTE.ink, ha="center", va="center", zorder=3)

    # Stacked inside the lens, so 1, 2 and 3 pairs read as visibly different
    # bond strengths rather than a relabelled dot.
    spread = 0.115 * (pairs - 1)
    for offset in np.linspace(-spread, spread, pairs):
        for dx in (-0.014, 0.014):
            axes.scatter([0.5 + dx], [CENTRE_Y + offset], s=155,
                         color=PALETTE.neutral_hue, zorder=4,
                         edgecolors=PALETTE.background, linewidths=1.6)

    axes.text(0.5, 0.975, f"{pairs} shared pair{'s' if pairs != 1 else ''}",
              fontsize=18, fontweight="bold", color=PALETTE.neutral_hue,
              ha="center", va="center")
    axes.text(0.5, 0.025, "inside both outer shells at once",
              fontsize=14, color=PALETTE.muted, ha="center", va="center")
