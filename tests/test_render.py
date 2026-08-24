"""Round 7 — crossfade arithmetic, G6 and G7.

Nothing here starts ffmpeg or matplotlib. The three things worth pinning are
all pure:

- the **timing arithmetic**, which is the one place a silent, systematic bug
  can live. A crossfade consumes time; get the correction wrong and every
  video is a fixed amount shorter than its narration, sync drifts scene by
  scene, and the only symptom is a video that feels slightly off. `render_demo`
  would still print "pass" if the tolerance were wide enough to swallow it.
- **G6**, against real PNG bytes written by the test itself.
- **G7**, against a stand-in for the ffprobe result, so all four branches are
  exercised including the ones a healthy pipeline never reaches.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.pipeline.gates import (
    DURATION_TOLERANCE_S,
    MIN_ARTIFACT_BYTES,
    check_g6,
    check_g7,
    png_size,
)
from app.providers.ffmpeg import (
    CROSSFADE_S,
    MuxScene,
    build_filtergraph,
    still_durations,
    xfade_offsets,
)

MEASURED = [11.3, 13.1, 12.9, 14.2, 12.2]
"""Real edge-tts scene durations plus the trailing pad, from a pH scale run."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def write_png(path: Path, width: int, height: int) -> Path:
    """A minimal but genuinely valid PNG.

    Written by hand rather than by matplotlib so the test asserts on the gate,
    not on the renderer that the gate exists to police.
    """
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body)))

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


@dataclass
class FakeProbe:
    """Stands in for `ffmpeg.ProbeResult`, structurally.

    G7 takes a `ProbedMedia` Protocol precisely so this is possible: the gate
    can be driven into every branch without producing a genuinely broken MP4
    for each one.
    """

    duration_s: float = 63.3
    size_bytes: int = 3_300_000
    has_video: bool = True
    has_audio: bool = True


def total_after_crossfades(stills: list[float],
                           crossfade: float = CROSSFADE_S) -> float:
    """What ffmpeg's chained xfade will actually output."""
    return sum(stills) - max(len(stills) - 1, 0) * crossfade


# ---------------------------------------------------------------------------
# crossfade arithmetic — SPEC.md §9.4
# ---------------------------------------------------------------------------

class TestCrossfadeTiming:
    def test_the_finished_video_is_exactly_as_long_as_its_narration(self):
        """The load-bearing assertion of the whole muxing stage.

        Audio-first timing means the narration is the source of truth. If the
        video does not land on it, the choice to measure rather than estimate
        has been thrown away at the last step.
        """
        stills = still_durations(MEASURED)
        assert total_after_crossfades(stills) == pytest.approx(sum(MEASURED))

    def test_a_naive_mux_would_have_lost_time(self):
        """Guards the correction itself, not just the result.

        Holding each still for exactly its audio length looks obviously right
        and is wrong by 1.6 seconds. Naming that here stops a future
        simplification from quietly reintroducing it.
        """
        assert total_after_crossfades(MEASURED) == pytest.approx(
            sum(MEASURED) - 4 * CROSSFADE_S
        )

    def test_each_transition_lands_in_the_trailing_silence(self):
        """The crossfade must finish before the next scene starts speaking.

        Scene k's audio ends `CROSSFADE_S` before its display duration does -
        that gap is the trailing pad. Starting the fade there means the picture
        has settled by the first word of scene k+1.
        """
        offsets = xfade_offsets(still_durations(MEASURED))
        expected = [
            sum(MEASURED[: k + 1]) - CROSSFADE_S for k in range(len(MEASURED) - 1)
        ]
        assert offsets == pytest.approx(expected)

    def test_offsets_are_strictly_increasing(self):
        offsets = xfade_offsets(still_durations(MEASURED))
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == len(offsets)

    def test_a_single_scene_needs_no_correction(self):
        assert still_durations([12.0]) == [12.0]
        assert xfade_offsets([12.0]) == []

    def test_no_scenes_is_not_a_crash(self):
        assert still_durations([]) == []
        assert xfade_offsets([]) == []

    @pytest.mark.parametrize("count", [2, 3, 4, 5])
    def test_the_identity_holds_for_any_scene_count(self, count):
        durations = MEASURED[:count]
        assert total_after_crossfades(
            still_durations(durations)
        ) == pytest.approx(sum(durations))


class TestFiltergraph:
    def _graph(self, count: int = 5) -> str:
        scenes = [
            MuxScene(still=Path(f"s{i}.png"), audio=Path(f"s{i}.mp3"),
                     duration_s=MEASURED[i])
            for i in range(count)
        ]
        graph, _ = build_filtergraph(scenes, width=1280, height=720, fps=30)
        return graph

    def test_audio_inputs_are_numbered_after_the_stills(self):
        """Five stills then five mp3s, so scene k's audio is input 5+k.

        An off-by-one here does not fail the encode - ffmpeg happily pairs the
        wrong audio with the wrong picture and produces a video where the
        narration describes the previous slide.
        """
        graph = self._graph()
        for index in range(5):
            assert f"[{5 + index}:a]" in graph

    def test_every_scene_is_faded_not_cut(self):
        assert self._graph().count("xfade=transition=fade") == 4

    def test_the_audio_is_loudness_normalised(self):
        """SPEC.md §13. TTS output level varies between runs of the same voice."""
        assert "loudnorm" in self._graph()

    def test_no_scene_is_zoomed(self):
        """Guards against reintroducing the tremble, which looked like polish.

        `zoompan` crops in whole input pixels. A 4% ramp over a 12-second scene
        moves the crop origin 0.136px per frame, so it holds for seven frames,
        jumps one, holds for eight, jumps again - and that uneven rhythm reads
        as trembling. It cannot be tuned away: one pixel per frame needs a
        14.6x source, and a 2x source needs a 39% zoom that would crop the
        captions off. The fix was to stop zooming, which also made the encode
        2.2x faster.
        """
        graph = self._graph()
        assert "zoompan" not in graph
        assert "scale=1280:720" in graph, "stills go straight to output size"
        assert "scale=2560:1440" not in graph, "no upscale left to zoom into"

    def test_a_single_scene_skips_the_concat(self):
        graph = self._graph(count=1)
        assert "concat=" not in graph
        assert "xfade" not in graph
        assert "loudnorm" in graph


# ---------------------------------------------------------------------------
# G6 — frames
# ---------------------------------------------------------------------------

class TestG6:
    def test_a_full_set_of_correctly_sized_frames_passes(self, tmp_path):
        frames = [write_png(tmp_path / f"s{i}.png", 1280, 720) for i in range(5)]
        assert check_g6(frames, expected=5, width=1280, height=720) is None

    def test_a_missing_frame_is_named(self, tmp_path):
        frames = [write_png(tmp_path / f"s{i}.png", 1280, 720) for i in range(4)]
        failure = check_g6(frames, expected=5, width=1280, height=720)
        assert failure.gate == "G6"
        assert failure.reason == "frame_count"

    def test_a_zero_byte_frame_is_caught(self, tmp_path):
        frames = [write_png(tmp_path / f"s{i}.png", 1280, 720) for i in range(4)]
        empty = tmp_path / "s4.png"
        empty.touch()
        failure = check_g6([*frames, empty], expected=5, width=1280, height=720)
        assert failure.reason == "frame_missing"

    def test_a_file_that_is_not_a_png_is_caught(self, tmp_path):
        frames = [write_png(tmp_path / f"s{i}.png", 1280, 720) for i in range(4)]
        bogus = tmp_path / "s4.png"
        bogus.write_bytes(b"not a png at all, but definitely non-empty" * 4)
        failure = check_g6([*frames, bogus], expected=5, width=1280, height=720)
        assert failure.reason == "frame_unreadable"

    def test_the_wrong_resolution_is_caught(self, tmp_path):
        """matplotlib does not raise on a mis-sized figure; it saves it."""
        frames = [write_png(tmp_path / f"s{i}.png", 1280, 720) for i in range(4)]
        small = write_png(tmp_path / "s4.png", 640, 360)
        failure = check_g6([*frames, small], expected=5, width=1280, height=720)
        assert failure.reason == "frame_dimensions"
        assert "640x360" in failure.detail

    def test_png_size_reads_the_ihdr_header(self, tmp_path):
        assert png_size(write_png(tmp_path / "a.png", 800, 450)) == (800, 450)

    def test_png_size_returns_none_for_a_missing_file(self, tmp_path):
        assert png_size(tmp_path / "nope.png") is None


# ---------------------------------------------------------------------------
# G7 — artifact
# ---------------------------------------------------------------------------

class TestG7:
    def test_a_healthy_artifact_passes(self):
        """Stated first on purpose: a gate that rejects everything is not a gate."""
        assert check_g7(FakeProbe(), expected_duration_s=63.3) is None

    def test_a_video_with_no_audio_stream_is_rejected(self):
        """R5 as a single check. A silent slideshow is the named failure mode."""
        failure = check_g7(FakeProbe(has_audio=False), expected_duration_s=63.3)
        assert failure.gate == "G7"
        assert failure.reason == "artifact_no_audio"

    def test_a_file_with_no_video_stream_is_rejected(self):
        failure = check_g7(FakeProbe(has_video=False), expected_duration_s=63.3)
        assert failure.reason == "artifact_no_video"

    def test_a_truncated_encode_is_rejected(self):
        failure = check_g7(FakeProbe(size_bytes=MIN_ARTIFACT_BYTES - 1),
                           expected_duration_s=63.3)
        assert failure.reason == "artifact_too_small"

    def test_duration_drift_beyond_tolerance_is_rejected(self):
        """Catches exactly the bug `still_durations` exists to prevent."""
        drifted = FakeProbe(duration_s=63.3 - DURATION_TOLERANCE_S - 0.5)
        failure = check_g7(drifted, expected_duration_s=63.3)
        assert failure.reason == "artifact_duration"

    def test_drift_inside_tolerance_is_allowed(self):
        """Container timestamps and AAC frame padding move the duration slightly.

        The tolerance absorbs encoder rounding, not a wrong calculation - the
        arithmetic tests above are what hold the sub-second accuracy.
        """
        nudged = FakeProbe(duration_s=63.3 + DURATION_TOLERANCE_S - 0.1)
        assert check_g7(nudged, expected_duration_s=63.3) is None

    def test_a_zero_byte_artifact_never_reports_completed(self):
        """CLAUDE.md §11 forbids it outright. This is the check that enforces it."""
        dead = FakeProbe(size_bytes=0, duration_s=0.0,
                         has_video=False, has_audio=False)
        assert check_g7(dead, expected_duration_s=63.3) is not None
