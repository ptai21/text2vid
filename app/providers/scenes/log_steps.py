"""Why one pH unit is a factor of ten.

The logarithm is the hardest idea in the pH concept and the beat G4 insists on.

The y axis is **logarithmic on purpose**, and that choice is the whole lesson.
On a linear axis a 1x bar beside a 100x bar is one percent of the height and
simply vanishes, which teaches nothing. On a log axis the bar tops form an even
staircase: equal steps along the pH axis are equal *multiplications* of
concentration. Every "x10" arrow is the same length, and the learner reads the
rule straight off the picture.

The axis itself stays unlabelled. Exponent ticks would be a second, competing
explanation of the same idea, and the "1x / 10x / 100x" callouts say it in
words a first-time learner already has.
"""

from __future__ import annotations

from typing import Any

from matplotlib.axes import Axes

from app.providers.theme import PALETTE

MAX_BARS = 5
BASE = 0.5
"""Bar floor. A log axis cannot start at zero, and drawing from just below 1
keeps the shortest bar visible without distorting the ratios above it."""


def draw(axes: Axes, params: dict[str, Any], ctx) -> None:
    start, end = params["from_ph"], params["to_ph"]
    step = 1 if end >= start else -1

    values = list(range(start, end + step, step))[:MAX_BARS]
    if len(values) < 2:  # a zero-width range still has to draw something
        values = [start, min(start + 1, 14)]

    # Concentration relative to the weakest bar shown, so the picture reads the
    # same whatever absolute pH values the script chose.
    weakest = max(values)
    tops = [10.0 ** (weakest - value) for value in values]
    tallest = max(tops)

    axes.set_yscale("log")
    axes.set_xlim(-0.7, len(values) - 0.3)
    axes.set_ylim(BASE * 0.9, tallest * 4)

    # set_yscale reinstates the log locator, undoing the blank ticks the shared
    # layout applied. Both minor and major have to go.
    axes.tick_params(axis="both", which="both", left=False, bottom=False,
                     labelleft=False, labelbottom=False)

    for index, (value, top) in enumerate(zip(values, tops)):
        colour = PALETTE.acid if value < 7 else (
            PALETTE.neutral_hue if value == 7 else PALETTE.base
        )
        # `height` is measured from `bottom`, so the top lands on the ratio
        # itself and the staircase stays even.
        axes.bar(index, top - BASE, width=0.5, bottom=BASE, color=colour,
                 zorder=2)

        multiple = 10 ** (weakest - value)
        axes.text(index, top * 1.25,
                  "1x" if multiple == 1 else f"{multiple:,}x",
                  fontsize=16, fontweight="bold", color=colour,
                  ha="center", va="bottom", zorder=3)

        axes.text(index, -0.06, f"pH {value}", fontsize=16, color=PALETTE.ink,
                  ha="center", va="top", fontweight="bold",
                  transform=axes.get_xaxis_transform())

        # Each arrow spans exactly one decade, and on a log axis every one of
        # them is the same length - which is the point being made.
        if index:
            axes.annotate(
                "", xy=(index - 0.27, top), xytext=(index - 0.73, tops[index - 1]),
                arrowprops=dict(arrowstyle="-|>", color=PALETTE.muted, lw=1.6,
                                shrinkA=2, shrinkB=2),
                zorder=4,
            )
            axes.text(index - 0.5, (tops[index - 1] * top) ** 0.5 * 1.12, "x10",
                      fontsize=14, color=PALETTE.muted, ha="center",
                      va="bottom", zorder=4)

    axes.text(0.0, 1.02, "hydrogen ion concentration, relative",
              fontsize=13, color=PALETTE.muted, ha="left", va="bottom",
              transform=axes.transAxes)
