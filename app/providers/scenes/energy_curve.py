"""Potential energy against internuclear distance.

This is the scene that answers *why* a bond forms at all: the curve has a
minimum, and systems settle into minima. Repulsion dominates on the left,
attraction on the right, and the labelled trough is the stable bond length.

A Morse well is drawn rather than a Lennard-Jones one. Both are correct
shapes, but the twelfth-power LJ wall is so steep that the minimum lands in
the leftmost tenth of the frame with a long flat tail filling the rest - the
learner's eye goes to the empty part. Morse puts the trough about a third of
the way in, where it is being pointed at.

The minimum is drawn **at** its value rather than animated into it (SPEC.md §7).
A still curve with a marked trough carries the same explanation as a moving
one, at none of the render cost.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from matplotlib.axes import Axes

from app.providers.theme import PALETTE

STIFFNESS = 2.0
LEFT_FACTOR, RIGHT_FACTOR = 0.35, 2.6


def draw(axes: Axes, params: dict[str, Any], ctx) -> None:
    label = params["label"]
    minimum = float(params["min_distance"])

    # The schema leaves `min_distance` unbounded because scripts legitimately
    # quote it in picometres (74) or angstroms (0.74). The curve is therefore
    # built in units of the bond length itself and only the axis is labelled
    # with the model's number, so both spellings draw identically.
    if not minimum > 0:
        minimum = 1.0

    distance = np.linspace(LEFT_FACTOR * minimum, RIGHT_FACTOR * minimum, 500)
    ratio = distance / minimum
    energy = (1.0 - np.exp(-STIFFNESS * (ratio - 1.0))) ** 2 - 1.0

    axes.set_xlim(distance[0], distance[-1])
    axes.set_ylim(-1.45, 2.0)  # the repulsive wall runs off the top, as it should

    axes.axhline(0, color=PALETTE.muted, linewidth=1, alpha=0.30)
    axes.fill_between(distance, energy, 0, where=(energy < 0),
                      color=PALETTE.neutral_hue, alpha=0.15, zorder=2)
    axes.plot(distance, energy, color=PALETTE.base, linewidth=3, zorder=3)

    axes.scatter([minimum], [-1.0], s=200, color=PALETTE.neutral_hue,
                 zorder=5, edgecolors=PALETTE.background, linewidths=2)
    axes.annotate(
        label, xy=(minimum, -1.06), xytext=(minimum, -1.34),
        fontsize=16, fontweight="bold", color=PALETTE.neutral_hue,
        ha="center", va="center",
        arrowprops=dict(arrowstyle="-", color=PALETTE.neutral_hue, lw=1.4),
    )
    axes.text(minimum, 0.06, f"{minimum:g}", fontsize=12, color=PALETTE.muted,
              ha="center", va="bottom")
    axes.plot([minimum, minimum], [-0.94, 0.0], color=PALETTE.neutral_hue,
              linewidth=1, alpha=0.45, linestyle=(0, (3, 3)), zorder=2)

    axes.text(distance[0] + 0.04 * minimum, 1.55, "too close:\nthey repel",
              fontsize=14, color=PALETTE.acid, ha="left", va="top",
              fontweight="bold", linespacing=1.4)
    axes.text(distance[-1], 0.30, "too far:\nweak attraction",
              fontsize=14, color=PALETTE.base, ha="right", va="bottom",
              fontweight="bold", linespacing=1.4)

    # Axis names live inside the frame. Placed below it they collide with the
    # caption zone, which is fixed by the shared layout grid and wins.
    axes.text(0.99, 0.02, "distance between nuclei", fontsize=13,
              color=PALETTE.muted, ha="right", va="bottom",
              transform=axes.transAxes)
    axes.text(-0.025, 0.5, "energy", fontsize=13, color=PALETTE.muted,
              ha="center", va="center", rotation=90,
              transform=axes.transAxes)
