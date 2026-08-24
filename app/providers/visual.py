"""Deterministic frame rendering — SPEC.md §9.3.

This stage contributes **zero** non-determinism. Same script in, same pixels
out, every time, on every machine. That is not a happy accident of matplotlib;
it is the reason programmatic rendering was chosen over a generative video API
in the first place (CLAUDE.md §4). A stage that cannot vary is a stage that
never needs a retry, a gate tuned by feel, or a second opinion.

**One PNG per scene. Five `savefig` calls per video.** Every frame matplotlib
draws is a frame ffmpeg could have produced for free, so all motion - the
crossfades - is deferred to the encoder (§9.4). The
difference is a full harness pass in roughly ten minutes rather than forty.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

import matplotlib.pyplot as plt

from app.config import Settings
from app.domain.script import Scene
from app.logging import get_logger
from app.providers import theme
from app.providers.scenes import SCENES, RenderContext

log = get_logger(__name__)


class RenderError(RuntimeError):
    """Maps to the `render_failed` FailureCode."""


class VisualProvider(Protocol):
    """SPEC.md §11. Swapping to a generative video provider is one new
    class satisfying this and one line in `main.py` - which is the answer
    to R9, stated as an interface rather than as a promise.

    `render` returns a *list* of frames even though the matplotlib
    implementation always returns one. The list is the seam: a provider
    that emits a frame sequence, or a clip decoded to frames, fits without
    the pipeline changing shape.
    """

    def render(self, scene: Scene, duration: float,
               out_dir: Path) -> list[Path]: ...


class MatplotlibProvider:
    """Satisfies `VisualProvider` (SPEC.md §11).

    Constructed **per job** rather than per process, because the title card
    shows the learner's original query and that differs by job. Building it
    here rather than threading a context argument through `render` keeps the
    provider Protocol in §11 exactly as specified - and the object is a few
    strings, so the per-job construction costs nothing.
    """

    def __init__(self, settings: Settings, context: RenderContext):
        self._width = settings.video_width
        self._height = settings.video_height
        self._context = context
        theme.apply()

    def render(self, scene: Scene, duration: float, out_dir: Path) -> list[Path]:
        """One scene, one PNG.

        `duration` is unused by this implementation and deliberately kept in
        the signature: it is what a frame-sequence or generative provider would
        need, and R9 asks that the seam be obvious rather than asserted. A
        provider swapped in here receives everything it requires.
        """
        drawer = SCENES.get(scene.visual.type)
        if drawer is None:
            # Unreachable while G3 runs, and that is the point: G3 guarantees
            # every type reaching this package can be drawn. If it ever fires,
            # the gate table and this package have drifted apart, and a named
            # failure says so far better than a KeyError.
            raise RenderError(
                f"No renderer for visual type {scene.visual.type!r}. G3 should "
                "have rejected this script before it reached the renderer."
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{scene.scene_id}.png"
        started = time.perf_counter()

        figure = theme.new_figure()
        try:
            theme.draw_heading(figure, scene.heading)
            axes = theme.content_axes(figure)
            drawer(axes, dict(scene.visual.params), self._context)
            theme.draw_caption(figure, scene.narration)

            figure.savefig(out, format="png", facecolor=figure.get_facecolor())
        except RenderError:
            raise
        except Exception as exc:  # noqa: BLE001 - becomes a named FailureCode
            raise RenderError(
                f"Scene {scene.scene_id} ({scene.visual.type}) failed to "
                f"draw: {exc!r}"
            ) from exc
        finally:
            # Figures are not garbage collected while pyplot holds a reference,
            # and a long harness run leaks every one of them otherwise.
            plt.close(figure)

        log.debug("render.scene", scene_id=scene.scene_id,
                  visual=scene.visual.type,
                  ms=round((time.perf_counter() - started) * 1000))
        return [out]
