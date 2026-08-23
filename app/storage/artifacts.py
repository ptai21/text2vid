"""Artifact storage — SPEC.md §12.

The artifact is a **bundle**, not a file: `video.mp4` for the learner,
`script.json` for what the model actually produced, `manifest.json` for the
run record. One directory per job, one class to swap for S3.

Synchronous on purpose: these are small local file operations, and the
callers that matter (the generator) already run them off the event loop in a
worker thread.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO, Protocol

VIDEO_NAME = "video.mp4"
SCRIPT_NAME = "script.json"
MANIFEST_NAME = "manifest.json"


class ArtifactStore(Protocol):
    def put(self, job_id: str, name: str, src: Path) -> str: ...

    def open(self, job_id: str, name: str) -> BinaryIO: ...

    def exists(self, job_id: str, name: str) -> bool: ...


class LocalArtifactStore:
    """Writes to `artifacts/{job_id}/`."""

    def __init__(self, root: Path):
        self._root = Path(root)

    def _path(self, job_id: str, name: str) -> Path:
        return self._root / job_id / name

    def put(self, job_id: str, name: str, src: Path) -> str:
        """Copy `src` into the bundle. Returns the stored location.

        Normalised with `as_posix()` because Windows backslashes break once
        the path is serialised into `manifest.json` (SETUP.md §10).
        """
        destination = self._path(job_id, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, destination)
        return destination.as_posix()

    def open(self, job_id: str, name: str) -> BinaryIO:
        return self._path(job_id, name).open("rb")

    def exists(self, job_id: str, name: str) -> bool:
        return self._path(job_id, name).is_file()
