"""One electron handed over, and the two ions that result.

Deliberately the visual mirror of `atom_pair`: same two shells, same accents,
same neutral colour on the electron in play. The difference is the only thing
that matters - here the shells are apart and an arrow crosses the gap, rather
than overlapping with the electrons resting inside both.

Placed next to each other the two scenes make the ionic/covalent distinction
without a sentence of narration, which is exactly the job of the comparison
concept.
"""

from __future__ import annotations

from typing import Any

from matplotlib.axes import Axes

from app.providers.theme import PALETTE, circle

RADIUS = 0.115
DONOR_X, ACCEPTOR_X = 0.24, 0.76
CENTRE_Y = 0.60


def draw(axes: Axes, params: dict[str, Any], ctx) -> None:
    donor, acceptor = params["donor"], params["acceptor"]

    axes.set_xlim(0, 1)
    axes.set_ylim(0, 1)

    # The shells are pushed apart rather than centred, so the gap the electron
    # crosses is the widest thing in the frame. In `atom_pair` the equivalent
    # space is an overlap; the contrast is the lesson.
    for x, label, colour, charge in (
        (DONOR_X, donor, PALETTE.acid, "+"),
        (ACCEPTOR_X, acceptor, PALETTE.base, "-"),
    ):
        axes.add_patch(circle((x, CENTRE_Y), RADIUS, facecolor=colour,
                              alpha=0.16, edgecolor=colour, linewidth=2.5,
                              zorder=1))
        axes.text(x, CENTRE_Y + 0.09, label, fontsize=28, fontweight="bold",
                  color=PALETTE.ink, ha="center", va="center", zorder=3)
        axes.text(x, CENTRE_Y - 0.13, f"charge {charge}", fontsize=16,
                  fontweight="bold", color=colour, ha="center", va="center",
                  zorder=3)

    axes.annotate(
        "", xy=(ACCEPTOR_X - RADIUS - 0.02, CENTRE_Y),
        xytext=(DONOR_X + RADIUS + 0.02, CENTRE_Y),
        arrowprops=dict(arrowstyle="-|>", color=PALETTE.neutral_hue, lw=2.8,
                        shrinkA=0, shrinkB=0),
        zorder=3,
    )
    axes.scatter([0.5], [CENTRE_Y], s=210, color=PALETTE.neutral_hue,
                 zorder=4, edgecolors=PALETTE.background, linewidths=2)
    axes.text(0.5, CENTRE_Y + 0.11, "one electron", fontsize=14,
              color=PALETTE.neutral_hue, ha="center", va="center",
              fontweight="bold")

    axes.text(0.5, 0.14, "transferred, not shared", fontsize=18,
              fontweight="bold", color=PALETTE.neutral_hue,
              ha="center", va="center")
    axes.text(0.5, 0.035, "opposite charges now attract", fontsize=14,
              color=PALETTE.muted, ha="center", va="center")
