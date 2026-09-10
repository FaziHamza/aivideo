"""End-to-end orchestration: one input video in, one validated 3-minute MP4 out.

    probe -> detect clips -> (optional vision check) -> plan -> warm reaction
    cache -> encode -> concat -> validate -> record

Batches run strictly one video at a time (req 9), so a 50-60 video day (req 10)
is a queue the UI can leave running, and a failure on video 12 never takes the
rest of the queue down with it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from . import ffmpeg, planner, rank, scenes, validate, vision
from .config import Settings, ensure_dirs
from .library import ReactionLibrary, next_output_path
from .models import Clip, JobResult, RenderPlan
from .renderer import RenderError, Renderer

# progress fraction 0..1, phase label
ProgressCb = Optional[Callable[[float, str], None]]
StatusCb = Optional[Callable[[str], None]]
CancelCb = Optional[Callable[[], bool]]

# How much of one job's progress bar each phase owns.
_WEIGHTS = (
    ("detect", 0.15),
    ("rate", 0.10),
    ("cache", 0.05),
    ("render", 0.65),
    ("validate", 0.05),
)


class PipelineError(RuntimeError):
    pass


def _log_job(source: Path, output, ok: bool, error: str,
             notes: list[str], elapsed: float) -> None:
    """Append one job's story to data/logs/app.log.

    This exists for exactly one scenario: the app is on someone else's
    machine, a video came out wrong, and the only way to see what the
    pipeline decided is a file they can send. Plain text, newest at the
    bottom, rotated at ~2 MB so it can never grow into a problem. The API
    key is never written anywhere near this.
    """
    from .config import DATA_DIR
    try:
        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / "app.log"
        if log.exists() and log.stat().st_size > 2_000_000:
            old_log = log_dir / "app.log.1"
            if old_log.exists():
                old_log.unlink()
            log.rename(old_log)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"[{stamp}] {'OK  ' if ok else 'FAIL'} {source}",
                 f"  output : {output or '-'}",
                 f"  took   : {elapsed:.0f}s"]
        if error:
            lines.append(f"  error  : {error}")
        for note in notes:
            lines.append(f"  note   : {note}")
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")
    except Exception:
        pass  # a logging problem must never touch a render


@dataclass
class BatchProgress:
    index: int          # 0-based position in the queue
    total: int
    source: Path
    fraction: float     # progress of the current video, 0..1
    phase: str

    @property
    def overall(self) -> float:
        if self.total <= 0:
            return 0.0
        return (self.index + self.fraction) / self.total


class Pipeline:
    """Reusable engine. Construct once, process many videos."""

    def __init__(self, settings: Settings | None = None,
                 library: ReactionLibrary | None = None):
        ensure_dirs()
        self.settings = settings or Settings.load()
        self.library = library or ReactionLibrary()
        self._renderer: Optional[Renderer] = None
        self._cache_signature = ""

    # ------------------------------------------------------------------
    @property
    def renderer(self) -> Renderer:
        signature = self.settings.render_signature()
        if self._renderer is None or self._cache_signature != signature:
            self._renderer = Renderer(self.settings, self.library)
            self._cache_signature = signature
        return self._renderer

    def warnings(self) -> list[str]:
        """Things worth telling the user that do not stop a render."""
        notes: list[str] = []
        try:
            import scenedetect  # noqa: F401
        except ImportError:
            notes.append(
                "PySceneDetect is not installed - using FFmpeg's scene filter. "
                "Install it (pip install scenedetect) for finer boundary "
                "detection."
            )
        from . import xtro
        keyed = xtro.configured()
        if (self.settings.clip_selection or "").lower() == "best" and not keyed:
            notes.append(
                "Clip selection is set to 'best' but no XtroEdge key is "
                "configured, so clips will be taken in order. Set "
                "XTROEDGE_API_KEY or put the key in data/xtroedge_key.txt."
            )
        if self.settings.vision_enabled and not keyed:
            notes.append(
                "Cut verification is on but no XtroEdge key is configured. "
                "Without it the pixel measures decide alone, and on the test "
                "footage they split 7 of 24 continuous shots - meaning a "
                "reaction can land in the middle of a shot."
            )
        elif not self.settings.vision_enabled:
            notes.append(
                "Cut verification is switched off. The pixel measures alone "
                "split 7 of 24 continuous shots on the test footage, so some "
                "reactions may land mid-shot."
            )
        missing = self.library.missing_files()
        if missing:
            notes.append(f"{len(missing)} reaction file(s) in the library are "
                         "missing from disk.")
        return notes

    def preflight(self) -> list[str]:
        """Problems that would stop a render, in plain language."""
        problems: list[str] = []
        try:
            ffmpeg.ffmpeg_path()
            ffmpeg.ffprobe_path()
        except ffmpeg.FFmpegMissing as exc:
            problems.append(str(exc))
        # PySceneDetect is preferred but not required: detection falls back to
        # FFmpeg's scene filter, so a missing package is not a blocker.
        if not self.library.active_reactions():
            if self.library.count() == 0:
                problems.append("The reaction library is empty - add reaction "
                                "videos first.")
            else:
                problems.append("No usable reactions: every entry is disabled "
                                "or its file is missing.")
        return problems

    # ------------------------------------------------------------------
    def plan_only(self, source: Path | str,
                  on_progress: ProgressCb = None,
                  on_status: StatusCb = None,
                  cancelled: CancelCb = None) -> tuple[RenderPlan, list[Clip]]:
        """Detect and plan without encoding, for a preview in the UI."""
        source = Path(source)
        status = on_status or (lambda _m: None)

        status(f"Reading {source.name}")
        info = ffmpeg.probe(source)
        if info.duration <= 0:
            raise PipelineError(f"{source.name} has no readable duration.")

        status("Detecting clip boundaries")
        clips, notes = scenes.detect_clips(
            source, self.settings,
            on_progress=(lambda f: on_progress(f, "detect")) if on_progress else None,
            cancelled=cancelled,
            verified=self._verifier_live(),
        )

        clips = self._verify_boundaries(clips, source, notes, status,
                                        cancelled=cancelled)
        clips = self._enforce_usable_clips(clips, notes)
        ratings = self._rate_clips(clips, source, notes, on_status=on_status,
                                   cancelled=cancelled)

        reactions = self.library.active_reactions()
        offset = self.library.get_rotation_offset()
        plan = planner.build_plan(clips, reactions, self.settings,
                                  rotation_offset=offset, ratings=ratings)
        plan.notes = list(notes) + list(plan.notes)
        return plan, clips

    def _rate_clips(self, clips: list[Clip], source: Path, notes: list[str],
                    on_status: StatusCb = None,
                    on_progress=None,
                    cancelled: CancelCb = None) -> Optional[dict[int, float]]:
        """Per-clip ratings for `best` selection, or None to fall back.

        None and an empty dict mean different things downstream: None says
        "no rating available, use clip order", while a dict says "these are the
        scores". Never return {} for a failure.
        """
        if (self.settings.clip_selection or "").lower() != "best":
            return None
        if not rank.available(self.settings):
            if self.settings.rank_clips:
                notes.append(
                    "Clip rating is on but no XtroEdge key is configured; "
                    "using clip order instead."
                )
            return None

        affordable, why = rank.affordable(self.settings)
        if not affordable:
            notes.append(why)
            return None

        if on_status:
            on_status(f"Rating {len(clips)} clips")
        ratings, rank_notes = rank.rate_clips(
            clips, source, self.settings, on_status=on_status,
            cancelled=cancelled)
        notes.extend(rank_notes)
        if on_progress:
            on_progress(1.0)
        return rank.scores_by_index(ratings) if ratings else None

    def _verifier_live(self) -> bool:
        """Whether the vision check will actually run for this job.

        Detection asks this before choosing its profile: the recall profile
        proposes extra boundaries on the explicit promise that a verifier will
        review them, so it must only be used when that promise can be kept -
        key present, switch on, and enough request budget left for the batch
        not to strand later videos.
        """
        from . import xtro
        if not self.settings.vision_enabled or not xtro.configured():
            return False
        remaining = xtro.remaining_requests(max_age=240)
        reserve = int(self.settings.rank_min_requests or 0)
        return remaining is None or remaining > reserve

    def _enforce_usable_clips(self, clips: list[Clip],
                              notes: list[str]) -> list[Clip]:
        """No clip may be longer than the whole target output.

        The detector already chops oversized stretches, but a giant clip can
        still arrive here: the vision check can merge a run of boundaries back
        into one, the non-histogram detectors do no chopping at all, and a
        video with no cuts comes back as one single clip. A clip longer than
        the target is unusable - the planner used to either skip it (losing
        most of the footage) or ship it whole with one reaction at the end,
        which is exactly the '7-minute video, one reaction' complaint. These
        last-resort splits are evenly spaced, because at this point there is
        no cut-likeness data left to choose better spots with.
        """
        target = float(self.settings.target_duration or 0.0)
        if target <= 0:
            return clips
        chunk = min(60.0, max(20.0, target / 4.0))
        out: list[Clip] = []
        added = 0
        for clip in clips:
            if clip.duration <= target:
                out.append(clip)
                continue
            pieces = max(2, int(clip.duration // chunk) + 1)
            span = clip.duration / pieces
            for i in range(pieces):
                start = clip.start + i * span
                end = clip.end if i == pieces - 1 else clip.start + (i + 1) * span
                out.append(Clip(source=clip.source, start=start, end=end,
                                index=0, confidence=0.5,
                                forced=(i > 0) or clip.forced))
            added += pieces - 1
        if not added:
            return clips
        notes.append(
            f"{added} split(s) forced after verification: a clip still "
            f"ran longer than the {target:g}s target, so it was divided "
            f"into ~{chunk:g}s pieces. Reactions land at those seams "
            "rather than not at all."
        )
        # Clip is frozen, so reindex by rebuilding rather than mutating.
        return [Clip(source=c.source, start=c.start, end=c.end, index=i,
                     confidence=c.confidence, forced=c.forced)
                for i, c in enumerate(out)]

    def _verify_boundaries(self, clips: list[Clip], source: Path, notes,
                           status: Callable,
                           cancelled: CancelCb = None) -> list[Clip]:
        """Run the vision check; if it cannot run, keep the promise anyway.

        When detection used the recall profile and the check then failed
        (network, quota mid-batch), the unreviewed extra boundaries are the
        false splits the recall profile knowingly produces - so this falls
        back to re-detecting with the conservative profile rather than
        shipping them. A missed cut reads as ordinary; a reaction mid-shot
        reads as broken.
        """
        if not self._verifier_live() or len(clips) < 2:
            return clips
        status(f"Verifying {len(clips) - 1} boundaries")
        verifier = vision.get_verifier(self.settings)
        failed = False
        try:
            checked = verifier.verify(clips, source, self.settings)
            failed = bool(getattr(verifier, "failed", False))
        except Exception:
            failed = True
        for note in getattr(verifier, "notes", []):
            notes.append(note)
        if not failed:
            return checked
        notes.append(
            "The cut check could not run, so detection was redone with the "
            "conservative profile instead of shipping unreviewed boundaries."
        )
        status("Cut check unavailable - re-detecting conservatively")
        fallback, _ = scenes.detect_clips(source, self.settings,
                                          cancelled=cancelled)
        return fallback

    # ------------------------------------------------------------------
    def process_one(self, source: Path | str,
                    output: Path | str | None = None,
                    on_progress: ProgressCb = None,
                    on_status: StatusCb = None,
                    cancelled: CancelCb = None) -> JobResult:
        """Run the whole pipeline for one input video."""
        source = Path(source)
        status = on_status or (lambda _m: None)
        started = time.perf_counter()
        weights = dict(_WEIGHTS)
        offsets = _phase_offsets()

        def phased(phase: str):
            if not on_progress:
                return None
            base, weight = offsets[phase], weights[phase]

            def _cb(fraction: float) -> None:
                on_progress(base + weight * max(0.0, min(1.0, fraction)), phase)
            return _cb

        try:
            problems = self.preflight()
            if problems:
                raise PipelineError(" ".join(problems))

            # --- detect ---
            status(f"Reading {source.name}")
            info = ffmpeg.probe(source)
            if info.duration <= 0:
                raise PipelineError(f"{source.name} has no readable duration.")
            if info.usable_duration < self.settings.target_duration:
                # Not fatal -- the user may want a shorter output -- but say so
                # now rather than after a render that could not have worked.
                status(f"Warning: {source.name} is only "
                       f"{info.usable_duration:.0f}s, shorter than the "
                       f"{self.settings.target_duration:g}s target.")
            status("Detecting clip boundaries")
            clips, notes = scenes.detect_clips(
                source, self.settings,
                on_progress=phased("detect"), cancelled=cancelled,
                verified=self._verifier_live(),
            )
            clips = self._verify_boundaries(clips, source, notes, status,
                                            cancelled=cancelled)
            clips = self._enforce_usable_clips(clips, notes)
            status(f"Found {len(clips)} clips")

            # --- rate (which clips are worth reacting to) ---
            ratings = self._rate_clips(clips, source, notes,
                                       on_status=on_status,
                                       on_progress=phased("rate"),
                                       cancelled=cancelled)

            # --- plan ---
            reactions = self.library.active_reactions()
            offset = self.library.get_rotation_offset()
            plan = planner.build_plan(clips, reactions, self.settings,
                                      rotation_offset=offset, ratings=ratings)
            plan.notes = list(notes) + list(plan.notes)
            status(f"Timeline: {plan.originals_used} clips + "
                   f"{plan.originals_used} reactions = "
                   f"{plan.total_duration:.1f}s")

            # --- reaction cache ---
            renderer = self.renderer
            reaction_cache = renderer.warm_reaction_cache(
                reactions, on_status=on_status,
                on_progress=phased("cache"), cancelled=cancelled,
            )

            # --- encode ---
            target = Path(output) if output else next_output_path(source,
                                                                  self.settings)
            renderer.render(plan, target, reaction_cache=reaction_cache,
                            on_status=on_status,
                            on_progress=phased("render"), cancelled=cancelled)

            # --- validate ---
            status("Validating output")
            report = validate.validate_output(target, self.settings, plan)
            cb = phased("validate")
            if cb:
                cb(1.0)

            elapsed = time.perf_counter() - started
            # Move the rotation on so the next video in the batch opens with a
            # different reaction (req 5).
            if self.settings.rotation_advances_per_job:
                self.library.set_rotation_offset(
                    planner.next_rotation_offset(plan, len(reactions)))

            all_notes = list(plan.notes)
            self.library.record_job(
                source=source, output=target, ok=report.ok,
                duration=report.duration, clips_used=plan.originals_used,
                size_bytes=report.size_bytes, elapsed=elapsed,
                error="; ".join(report.problems),
                notes="\n".join(all_notes),
            )
            _log_job(source, target, report.ok,
                     "; ".join(report.problems), all_notes, elapsed)
            status("Done" if report.ok else "Finished with validation warnings")
            return JobResult(ok=report.ok, source=source, output=target,
                             plan=plan, validation=report, elapsed=elapsed,
                             # Carry the reason so the CLI and the results
                             # table can say why, not just that it failed.
                             error="" if report.ok else "; ".join(report.problems))

        except ffmpeg.Cancelled as exc:
            return JobResult(ok=False, source=source,
                             elapsed=time.perf_counter() - started,
                             error=str(exc) or "Cancelled")
        except scenes.DetectionCancelled:
            return JobResult(ok=False, source=source,
                             elapsed=time.perf_counter() - started,
                             error="Cancelled")
        except (PipelineError, planner.PlanningError, scenes.DetectionError,
                RenderError, ffmpeg.FFmpegError, OSError, ValueError) as exc:
            elapsed = time.perf_counter() - started
            message = str(exc) or exc.__class__.__name__
            try:
                self.library.record_job(source=source, output=None, ok=False,
                                        elapsed=elapsed, error=message)
                _log_job(source, None, False, message, [], elapsed)
            except Exception:
                pass
            status(f"Failed: {message}")
            return JobResult(ok=False, source=source, elapsed=elapsed,
                             error=message)

    # ------------------------------------------------------------------
    def process_batch(self, sources: Sequence[Path | str],
                      on_batch_progress: Optional[Callable[[BatchProgress], None]] = None,
                      on_status: StatusCb = None,
                      on_job_done: Optional[Callable[[int, JobResult], None]] = None,
                      cancelled: CancelCb = None) -> list[JobResult]:
        """Process a queue one video at a time (req 9)."""
        results: list[JobResult] = []
        total = len(sources)

        for index, source in enumerate(sources):
            if cancelled and cancelled():
                break
            source = Path(source)

            def _progress(fraction: float, phase: str, _i=index,
                          _s=source) -> None:
                if on_batch_progress:
                    on_batch_progress(BatchProgress(
                        index=_i, total=total, source=_s,
                        fraction=fraction, phase=phase))

            result = self.process_one(source, on_progress=_progress,
                                      on_status=on_status, cancelled=cancelled)
            results.append(result)
            if on_job_done:
                on_job_done(index, result)

        return results


def _phase_offsets() -> dict[str, float]:
    offsets: dict[str, float] = {}
    running = 0.0
    for name, weight in _WEIGHTS:
        offsets[name] = running
        running += weight
    return offsets


def summarise(results: Iterable[JobResult]) -> str:
    results = list(results)
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    lines = [f"{len(ok)}/{len(results)} videos rendered and validated."]
    total_time = sum(r.elapsed for r in results)
    if ok:
        lines.append(f"Average {total_time / len(results):.1f}s per video "
                     f"({total_time / 60:.1f} min total).")
    for r in bad:
        lines.append(f"FAILED {r.source.name}: {r.error}")
    return "\n".join(lines)
