"""Visual design system — SPEC.md §13.

Every rcParam, colour and zone in this file is paid for **once** and applies
to all three concepts and every future run. That is why SPEC.md §13 calls this
the place where "cheapest reasonable cost" is actually earned: unlike a
generative video API, quality here has no per-artifact price.

Two rules drive everything below.

**No matplotlib defaults.** A chart that looks like a matplotlib chart reads as
a plot someone pasted into a video. Spines, tick marks and the default blue are
all removed deliberately.

**One fixed layout grid.** Heading on top, content in the middle, caption at the
bottom - identical coordinates in all eight scene types. A shared grid is what
makes five unrelated diagrams read as one designed video rather than five
assembled images.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

import matplotlib

# Agg before pyplot: the renderer runs inside a worker thread with no display,
# and importing the default backend there is a hang, not an error (CLAUDE.md §6).
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Ellipse  # noqa: E402

DPI = 100
"""1280x720 at 100 dpi is figsize (12.8, 7.2). Keeping dpi at 100 means figure
coordinates and pixels differ by a constant, so the layout zones below can be
reasoned about in either unit."""


@dataclass(frozen=True)
class Palette:
    """Three accents plus a neutral, as SPEC.md §13 requires.

    Three is a constraint, not a shortage: it forces each colour to carry a
    consistent meaning across scenes. `acid` is always the low/donating side,
    `base` always the high/accepting side, `neutral_hue` always the balanced
    middle. A learner who sees orange in scene 2 and orange in scene 5 is being
    told those two things are related, and they are.
    """

    background: str = "#0F1621"
    surface: str = "#1A2433"
    ink: str = "#F2F5F9"
    muted: str = "#8A97A8"

    acid: str = "#F4A259"
    base: str = "#4EA8DE"
    neutral_hue: str = "#5FD3A6"


PALETTE = Palette()

ACCENTS = (PALETTE.acid, PALETTE.base, PALETTE.neutral_hue)

FONT_FAMILY = "DejaVu Sans"
"""Ships with matplotlib. Naming a font the machine may not have is how a
render silently falls back to a different typeface on someone else's box -
a determinism leak in a stage whose whole selling point is determinism."""


# ---------------------------------------------------------------------------
# Layout grid — figure coordinates, identical for every scene type
# ---------------------------------------------------------------------------

HEADING_Y = 0.90
CONTENT_RECT = (0.08, 0.30, 0.84, 0.48)  # left, bottom, width, height
CAPTION_TOP_Y = 0.205

CAPTION_WRAP_CHARS = 78
CAPTION_LINE_STEP = 0.042
MAX_CAPTION_LINES = 4


CONTENT_ASPECT = (CONTENT_RECT[2] * 12.8) / (CONTENT_RECT[3] * 7.2)
"""How many times wider than tall the content zone is (about 3.1).

Needed because `set_aspect(1)` would be the obvious way to draw a circle and is
the wrong one here: it resizes the axes box, which silently breaks the shared
layout grid this module exists to enforce. Scenes call `circle()` instead and
the grid stays fixed."""


def circle(centre: tuple[float, float], radius_x: float, **kwargs) -> Ellipse:
    """A patch that *looks* circular inside the wide content zone.

    `radius_x` is in x data units on a 0-1 axis; the y radius is stretched by
    the zone's aspect so the result renders round.
    """
    return Ellipse(
        centre,
        width=2 * radius_x,
        height=2 * radius_x * CONTENT_ASPECT,
        **kwargs,
    )


def apply() -> None:
    """Set rcParams globally. Idempotent, so calling it per render is safe."""
    plt.rcParams.update({
        "figure.figsize": (12.8, 7.2),
        "figure.dpi": DPI,
        "savefig.dpi": DPI,
        "figure.facecolor": PALETTE.background,
        "savefig.facecolor": PALETTE.background,
        "axes.facecolor": PALETTE.background,
        "font.family": FONT_FAMILY,
        "text.color": PALETTE.ink,
        "axes.labelcolor": PALETTE.muted,
        "axes.edgecolor": PALETTE.muted,
        "xtick.color": PALETTE.muted,
        "ytick.color": PALETTE.muted,
        "xtick.direction": "out",
        "ytick.direction": "out",
        "axes.grid": False,
        "grid.color": PALETTE.muted,
        "grid.alpha": 0.14,
        "grid.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.spines.left": False,
        "axes.spines.bottom": False,
        "lines.solid_capstyle": "round",
        "lines.antialiased": True,
        "patch.linewidth": 0,
        "legend.frameon": False,
    })


def new_figure() -> Figure:
    """A blank 1280x720 canvas with the background already painted."""
    apply()
    figure = plt.figure()
    figure.patch.set_facecolor(PALETTE.background)
    return figure


def content_axes(figure: Figure) -> plt.Axes:
    """The middle zone. Every scene type draws here and nowhere else."""
    axes = figure.add_axes(CONTENT_RECT)
    axes.set_facecolor(PALETTE.background)
    axes.set_xticks([])
    axes.set_yticks([])
    for spine in axes.spines.values():
        spine.set_visible(False)
    return axes


def draw_heading(figure: Figure, text: str) -> None:
    """Top zone. 1-6 words by schema, so it never needs wrapping."""
    figure.text(
        0.08, HEADING_Y, text,
        fontsize=30, fontweight="bold", color=PALETTE.ink,
        ha="left", va="center",
    )
    figure.add_artist(plt.Line2D(
        [0.08, 0.20], [HEADING_Y - 0.055, HEADING_Y - 0.055],
        color=PALETTE.base, linewidth=3, solid_capstyle="round",
        transform=figure.transFigure,
    ))


def draw_caption(figure: Figure, narration: str) -> None:
    """Burn the scene's narration into the bottom zone.

    SPEC.md §13 calls this the highest value-per-effort item in the whole
    design section, for three reasons at once: it raises perceived quality, it
    is genuine accessibility, and it lets an evaluator follow the demo **with
    the sound off** - which is how a take-home actually gets reviewed.
    """
    lines = textwrap.wrap(narration.strip(), width=CAPTION_WRAP_CHARS)

    # 38 words is the schema ceiling and wraps to four lines here. Truncating
    # rather than overflowing keeps the caption inside its zone: a caption that
    # runs off the bottom of the frame is worse than one that ends in an
    # ellipsis, and G2 makes the ellipsis case unreachable in practice.
    if len(lines) > MAX_CAPTION_LINES:
        lines = lines[:MAX_CAPTION_LINES]
        lines[-1] = lines[-1].rstrip(" ,.;") + "..."

    figure.patch.set_facecolor(PALETTE.background)
    for index, line in enumerate(lines):
        figure.text(
            0.5, CAPTION_TOP_Y - index * CAPTION_LINE_STEP, line,
            fontsize=17, color=PALETTE.ink, alpha=0.92,
            ha="center", va="center",
        )
