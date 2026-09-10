"""Qt worker threads.

Everything slow -- probing, detection, encoding -- runs here so the window
never freezes. The workers own no UI; they only emit signals, which keeps the
pipeline itself free of any Qt dependency.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.config import Settings
from core.library import ReactionLibrary
from core.models import JobResult
from core.pipeline import BatchProgress, Pipeline


class BatchWorker(QThread):
    """Renders a queue of videos, one at a time (req 9)."""

    progress = Signal(int, int, float, str)   # index, total, fraction, phase
    status = Signal(str)
    job_done = Signal(int, object)            # index, JobResult
    all_done = Signal(list)                   # list[JobResult]
    crashed = Signal(str)

    def __init__(self, sources: list[Path], settings: Settings,
                 library: ReactionLibrary, parent=None):
        super().__init__(parent)
        self.sources = list(sources)
        self.settings = settings
        self.library = library
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def cancelled(self) -> bool:
        return self._stop

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            pipe = Pipeline(self.settings, self.library)
            total = len(self.sources)

            def _progress(bp: BatchProgress) -> None:
                self.progress.emit(bp.index, bp.total, bp.fraction, bp.phase)

            def _done(index: int, result: JobResult) -> None:
                self.job_done.emit(index, result)

            results = pipe.process_batch(
                self.sources,
                on_batch_progress=_progress,
                on_status=self.status.emit,
                on_job_done=_done,
                cancelled=self.cancelled,
            )
            if total and not results:
                self.status.emit("Cancelled before the first video started.")
            self.all_done.emit(results)
        except Exception as exc:  # a crash here must not kill the window
            self.crashed.emit(f"{exc.__class__.__name__}: {exc}")


class PlanWorker(QThread):
    """Detects and plans one video without encoding, for the preview."""

    progress = Signal(float, str)
    status = Signal(str)
    ready = Signal(object, object)   # RenderPlan, list[Clip]
    crashed = Signal(str)

    def __init__(self, source: Path, settings: Settings,
                 library: ReactionLibrary, parent=None):
        super().__init__(parent)
        self.source = Path(source)
        self.settings = settings
        self.library = library
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        try:
            pipe = Pipeline(self.settings, self.library)
            plan, clips = pipe.plan_only(
                self.source,
                on_progress=lambda f, ph: self.progress.emit(f, ph),
                on_status=self.status.emit,
                cancelled=lambda: self._stop,
            )
            self.ready.emit(plan, clips)
        except Exception as exc:
            self.crashed.emit(str(exc) or exc.__class__.__name__)


class AddReactionsWorker(QThread):
    """Probes and stores reaction files (req 12) off the UI thread."""

    status = Signal(str)
    done = Signal(int, list)   # added count, error messages
    crashed = Signal(str)

    def __init__(self, paths: list[Path], library: ReactionLibrary,
                 replace: bool = False, parent=None):
        super().__init__(parent)
        self.paths = list(paths)
        self.library = library
        self.replace = replace

    def run(self) -> None:  # noqa: D102
        try:
            self.status.emit(f"Reading {len(self.paths)} reaction file(s)...")
            if self.replace:
                added, errors = self.library.replace_all(self.paths)
            else:
                added, errors = self.library.add_many(self.paths)
            self.done.emit(len(added), errors)
        except Exception as exc:
            self.crashed.emit(str(exc) or exc.__class__.__name__)
