"""Automatic clip-boundary detection (req 3) with doubtful-boundary scoring.

The long input video is a concatenation of separate clips. PySceneDetect finds
the cuts; each cut carries a confidence derived from how far the frame-content
metric cleared the detector threshold. Boundaries that only just cleared it are
the ones a Vision LLM should double-check later (req 11) -- detection itself
never blocks on that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .config import Settings
from .models import Clip

ProgressCb = Optional[Callable[[float], None]]  # 0.0 .. 1.0
CancelCb = Optional[Callable[[], bool]]


class DetectionError(RuntimeError):
    pass


class DetectionCancelled(RuntimeError):
    pass


# A boundary whose metric is within this ratio of the bare threshold is
# "only just" a cut, so we flag it as uncertain.
_DOUBT_MARGIN = 1.25
_METRIC_KEYS = ("content_val", "delta_hsv_avg", "adaptive_ratio")


# PySceneDetect changed shape between 0.6 and 0.7: 0.7 hands the callback a
# FrameTimecode where 0.6 passed a plain int, renamed get_seconds() to a
# `seconds` property, and returns frame_rate as a Fraction. These read both.
def _tc_seconds(value) -> float:
    for attr in ("seconds",):
        got = getattr(value, attr, None)
        if isinstance(got, (int, float)):
            return float(got)
    getter = getattr(value, "get_seconds", None)
    if callable(getter):
        return float(getter())
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _tc_frames(value) -> int:
    got = getattr(value, "frame_num", None)
    if isinstance(got, int):
        return got
    getter = getattr(value, "get_frames", None)
    if callable(getter):
        return int(getter())
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def detect_clips(source: str | Path, settings: Settings,
                 on_progress: ProgressCb = None,
                 cancelled: CancelCb = None,
                 verified: bool = False) -> tuple[list[Clip], list[str]]:
    """Split `source` into its constituent clips.

    Returns (clips, notes). Clips are in chronological order and never
    overlap, so downstream ordering guarantees (req 6) hold by construction.

    `verified=True` says a vision check will review every boundary afterwards,
    so the histogram detector may run at higher recall and leave precision to
    the verifier. Pass it only when that check will actually run: without it
    the extra boundaries it proposes are exactly the false splits the
    conservative profile exists to avoid.

    Uses PySceneDetect when available, otherwise FFmpeg's own scene filter --
    the tool stays usable with FFmpeg alone.
    """
    source = Path(source)
    detector = (settings.detector or "histogram").lower()

    if detector == "histogram":
        return detect_clips_histogram(source, settings, on_progress, cancelled,
                                      verified=verified)
    if detector == "ffmpeg":
        return detect_clips_ffmpeg(source, settings, on_progress, cancelled)

    try:
        from scenedetect import (AdaptiveDetector, ContentDetector,  # noqa: I001
                                 SceneManager, StatsManager, open_video)
    except ImportError:
        clips, notes = detect_clips_ffmpeg(source, settings, on_progress,
                                           cancelled)
        notes.insert(0, "PySceneDetect not installed - used FFmpeg's scene "
                        "filter instead.")
        return clips, notes

    notes: list[str] = []

    try:
        video = open_video(str(source))
    except Exception as exc:
        raise DetectionError(f"Cannot open {source.name}: {exc}") from exc

    fps = float(video.frame_rate or 30.0)  # may arrive as a Fraction
    total_seconds = 0.0
    if video.duration is not None:
        total_seconds = _tc_seconds(video.duration)

    min_len_frames = max(1, int(settings.min_scene_len * fps))
    stats = StatsManager()
    manager = SceneManager(stats_manager=stats)

    if (settings.detector or "content").lower() == "adaptive":
        detector = AdaptiveDetector(min_scene_len=min_len_frames)
    else:
        detector = ContentDetector(threshold=settings.scene_threshold,
                                   min_scene_len=min_len_frames)
    manager.add_detector(detector)

    if settings.downscale and settings.downscale > 0:
        manager.auto_downscale = False
        manager.downscale = settings.downscale
    else:
        manager.auto_downscale = True

    # PySceneDetect exposes no frame-level progress hook, but it calls us once
    # per detected scene -- position of that scene is a good enough estimate.
    def _on_scene(_frame_img, frame_num) -> None:
        if cancelled and cancelled():
            raise DetectionCancelled("scene detection cancelled")
        if on_progress and total_seconds > 0:
            at = _tc_seconds(frame_num) or (_tc_frames(frame_num) / fps)
            on_progress(min(0.99, at / total_seconds))

    try:
        manager.detect_scenes(video, show_progress=False, callback=_on_scene)
    except DetectionCancelled:
        raise
    except Exception as exc:
        raise DetectionError(f"Scene detection failed on {source.name}: {exc}") from exc

    scene_list = manager.get_scene_list()
    if on_progress:
        on_progress(1.0)

    if not scene_list:
        # Single continuous shot: treat the whole file as one clip rather than
        # inventing cuts that are not there.
        if total_seconds <= 0:
            raise DetectionError(f"Could not read any frames from {source.name}")
        notes.append(
            "No cuts detected - treating the whole video as a single clip. "
            "Lower the scene threshold if it really does contain separate clips."
        )
        return [Clip(source=source, start=0.0, end=total_seconds, index=0,
                     confidence=0.5)], notes

    threshold = float(settings.scene_threshold) or 27.0
    clips: list[Clip] = []
    dropped_short = 0
    dropped_long = 0

    for i, (start_tc, end_tc) in enumerate(scene_list):
        start = _tc_seconds(start_tc)
        end = _tc_seconds(end_tc)
        duration = end - start

        if settings.min_clip_duration and duration < settings.min_clip_duration:
            dropped_short += 1
            continue
        if settings.max_clip_duration and duration > settings.max_clip_duration:
            dropped_long += 1
            continue

        confidence = _boundary_confidence(stats, _tc_frames(start_tc), threshold)
        clips.append(Clip(source=source, start=start, end=end,
                          index=len(clips), confidence=confidence,
                          forced=round(start, 3) in (forced_starts or ())))

    if dropped_short:
        notes.append(
            f"Dropped {dropped_short} clip(s) shorter than "
            f"{settings.min_clip_duration:g}s."
        )
    if dropped_long:
        notes.append(
            f"Dropped {dropped_long} clip(s) longer than "
            f"{settings.max_clip_duration:g}s."
        )
    if not clips:
        raise DetectionError(
            f"{len(scene_list)} scenes found in {source.name} but all were "
            "filtered out by the min/max clip duration limits."
        )

    doubtful = sum(1 for c in clips if c.confidence < settings.vision_confidence_floor)
    if doubtful:
        notes.append(
            f"{doubtful} of {len(clips)} boundaries are uncertain "
            "(Vision LLM verification would target these)."
        )
    return clips, notes


def _boundary_confidence(stats, frame_num: int, threshold: float) -> float:
    """How decisively the cut at `frame_num` cleared the detector threshold."""
    if frame_num <= 0:
        return 1.0  # start of file is a certain boundary
    metric = None
    for key in _METRIC_KEYS:
        try:
            values = stats.get_metrics(frame_num, [key])
        except Exception:
            continue
        if values and values[0] is not None:
            metric = float(values[0])
            break
    if metric is None or threshold <= 0:
        return 1.0

    ratio = metric / threshold
    if ratio >= _DOUBT_MARGIN:
        return 1.0
    # ratio 1.0 (bare pass) -> 0.4, ratio 1.25 -> 1.0
    return max(0.0, min(1.0, 0.4 + 0.6 * (ratio - 1.0) / (_DOUBT_MARGIN - 1.0)))


def doubtful_clips(clips: list[Clip], settings: Settings) -> list[Clip]:
    """Boundaries worth sending to a Vision LLM (req 11)."""
    floor = settings.vision_confidence_floor
    return [c for c in clips if c.confidence < floor]


# --------------------------------------------------------------------------
# FFmpeg-only detector
# --------------------------------------------------------------------------
def detect_clips_ffmpeg(source: Path, settings: Settings,
                        on_progress: ProgressCb = None,
                        cancelled: CancelCb = None
                        ) -> tuple[list[Clip], list[str]]:
    """Detect cuts with FFmpeg's scene filter, no PySceneDetect needed.

    The frame is scaled down before comparison, so this runs decode-bound
    rather than compare-bound and stays fast on 1080p sources.
    """
    import subprocess

    from . import ffmpeg as ff

    notes: list[str] = []
    info = ff.probe(source)
    duration = info.usable_duration
    if duration <= 0:
        raise DetectionError(f"Could not read a duration from {source.name}")

    threshold = float(settings.ffmpeg_scene_threshold or 0.30)
    graph = (f"scale=480:-2,select='gt(scene,{threshold:.4f})',"
             f"metadata=print:file=-")
    cmd = [
        ff.ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-i", str(source),
        "-an", "-sn", "-dn",
        "-filter:v", graph,
        "-f", "null", "-",
    ]

    cuts: list[tuple[float, float]] = []  # (time, scene score)
    pending_time: Optional[float] = None

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            creationflags=ff._NO_WINDOW)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if cancelled and cancelled():
                proc.terminate()
                raise DetectionCancelled("scene detection cancelled")

            if line.startswith("frame:") and "pts_time:" in line:
                try:
                    pending_time = float(line.split("pts_time:")[1].split()[0])
                except (IndexError, ValueError):
                    pending_time = None
            elif line.startswith("lavfi.scene_score="):
                try:
                    score = float(line.split("=", 1)[1])
                except ValueError:
                    score = threshold
                if pending_time is not None:
                    cuts.append((pending_time, score))
                    pending_time = None
            elif line.startswith(("out_time_us=", "out_time_ms=")):
                if on_progress:
                    try:
                        secs = float(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        continue
                    on_progress(min(0.99, secs / duration))
    finally:
        if proc.stdout:
            proc.stdout.close()

    if proc.wait() != 0:
        raise DetectionError(f"FFmpeg scene detection failed on {source.name}")
    if on_progress:
        on_progress(1.0)

    return _clips_from_cuts(source, cuts, duration, threshold, settings,
                            notes)


def _clips_from_cuts(source: Path, cuts: list[tuple[float, float]],
                     duration: float, threshold: float, settings: Settings,
                     notes: list[str]) -> tuple[list[Clip], list[str]]:
    """Turn cut timestamps into non-overlapping clips."""
    min_len = max(0.1, float(settings.min_scene_len or 0.0))

    # Drop cuts that sit too close to the previous one: a flash or a fast pan
    # inside one clip should not split it.
    kept: list[tuple[float, float]] = []
    last = 0.0
    for when, score in sorted(cuts):
        if when - last >= min_len and duration - when >= min_len:
            kept.append((when, score))
            last = when

    if not kept:
        notes.append(
            "No cuts detected - treating the whole video as a single clip. "
            "Lower the scene threshold if it really does contain separate clips."
        )
        return [Clip(source=source, start=0.0, end=duration, index=0,
                     confidence=0.5)], notes

    boundaries = [(0.0, 1.0)] + kept
    clips: list[Clip] = []
    dropped_short = dropped_long = 0

    for i, (start, score) in enumerate(boundaries):
        # boundaries[i] starts where cut i-1 landed, so it ends at the next cut
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else duration
        span = end - start

        if settings.min_clip_duration and span < settings.min_clip_duration:
            dropped_short += 1
            continue
        if settings.max_clip_duration and span > settings.max_clip_duration:
            dropped_long += 1
            continue

        ratio = (score / threshold) if threshold > 0 else 2.0
        confidence = (1.0 if i == 0 else
                      max(0.0, min(1.0, 0.4 + 0.6 * (ratio - 1.0) /
                                   (_DOUBT_MARGIN - 1.0))))
        clips.append(Clip(source=source, start=start, end=end,
                          index=len(clips), confidence=confidence))

    if dropped_short:
        notes.append(f"Dropped {dropped_short} clip(s) shorter than "
                     f"{settings.min_clip_duration:g}s.")
    if dropped_long:
        notes.append(f"Dropped {dropped_long} clip(s) longer than "
                     f"{settings.max_clip_duration:g}s.")
    if not clips:
        raise DetectionError(
            f"{len(boundaries)} scenes found in {source.name} but all were "
            "filtered out by the min/max clip duration limits."
        )

    doubtful = sum(1 for c in clips
                   if c.confidence < settings.vision_confidence_floor)
    if doubtful:
        notes.append(f"{doubtful} of {len(clips)} boundaries are uncertain "
                     "(Vision LLM verification would target these).")
    return clips, notes


# --------------------------------------------------------------------------
# Histogram detector (default)
# --------------------------------------------------------------------------
# Adjacent-frame difference - what FFmpeg's scene filter and PySceneDetect's
# ContentDetector both measure - cannot tell a cut from fast camera motion: a
# pan across a bright scene moves as many pixels as a cut does. On the sample
# inputs that mislabelled 17 of 27 boundaries, so one real clip was split into
# three and each piece was handed its own reaction.
#
# This detector compares the third of a second BEFORE a candidate against the
# third of a second AFTER it. A pan or a shake barely changes that; a cut to
# different footage changes it a lot.
#
# It compares a 4x4 grid of per-cell histograms rather than one histogram for
# the whole frame. A whole-frame histogram only knows which colours are
# present, so an outdoor shot of an orange digger and an indoor shot against an
# orange wall look nearly identical to it - a real cut in the sample footage
# that it scored at 0.31, below any threshold that did not also split clips in
# half. The grid also knows roughly where the colours are. Measured on 24
# boundaries labelled by eye from the real inputs:
#
#   whole-frame histogram: best threshold caught 6 of 9 cuts, 0 false splits
#   4x4 grid            : best threshold caught 9 of 9 cuts, 2 false splits
#
# Three missed cuts leave three long stretches each holding two scenes and one
# reaction, which is worse than two reactions landing mid-scene, so the grid
# wins. Real cuts scored 0.52-0.76 and same-scene boundaries 0.17-0.63, hence
# the 0.52 default.
#
# It all comes from one decode pass at the output frame rate, so it costs about
# the same as the old detector and every cut already lands on an output frame.
_HIST_SIZE = 64          # frames are scaled to this before histogramming
_HIST_GRID = 4           # 4x4 grid of cells per frame
_HIST_BINS = 8           # bins per colour channel per cell
_ADJACENT_MIN = 0.10     # below this nothing is moving; skip the window check
_CANDIDATE_FLOOR = 0.15  # kept as a possible split point even if not a cut
# Auto threshold: how far above Otsu's split to sit, and the range it may take.
_AUTO_MARGIN = 1.15
_AUTO_MIN = 0.35
_AUTO_MAX = 0.65
_AUTO_FALLBACK = 0.47    # too few candidates to judge a spread from
# The `verified` profile, used when every boundary will be shown to the vision
# model afterwards. The conservative numbers above exist because a false split
# used to be unfixable; with a verifier behind it the detector's job flips to
# recall - propose every plausible cut, let the model throw out the wrong
# ones. Missed cuts are the one mistake the verifier cannot repair, because it
# can only merge boundaries that exist, never invent one.
_AUTO_MARGIN_VERIFIED = 1.00   # sit at Otsu's split rather than above it
_AUTO_MIN_VERIFIED = 0.30
_AUTO_MAX_VERIFIED = 0.58
_SPACING_VERIFIED = 0.5        # seconds between candidate peaks
_MERGE_FRACTION_VERIFIED = 0.50
# Not lower: the verifier samples frames 0.5s either side of a boundary, so
# two boundaries closer than ~0.8s make each other's frame pair straddle a
# second cut and the model ends up judging the wrong pair of shots. 0.90 at
# margin 0.90 produced 70 one-second slivers and the check drowned in them.
_MERGE_MIN_VERIFIED = 0.8
# Auto sliver length: this fraction of the median clip length, within a range.
# Swept against both measurements: 0.35 left 7 of 8 known spurious boundaries
# in place, 0.55 left 3, 0.65 left 2, and 0.75 left 2 but cost recall. 0.65 is
# where the spurious ones stop falling and before real cuts start going, which
# is the trade this tool wants - a lost cut gives one clip two scenes and one
# reaction, a spurious one drops a reaction into the middle of a shot.
_MERGE_FRACTION = 0.65
_MERGE_MIN = 0.6
_MERGE_MAX = 4.0
_WINDOW = 0.30           # seconds averaged either side of a candidate
# Reported as "weakly scored" in the job notes. Nothing branches on it.
_BORDERLINE = 0.55
_GAP = 0.10              # seconds skipped either side, to clear the transition


def detect_clips_histogram(source: Path, settings: Settings,
                           on_progress: ProgressCb = None,
                           cancelled: CancelCb = None,
                           verified: bool = False
                           ) -> tuple[list[Clip], list[str]]:
    """Detect real cuts by colour-histogram change across each candidate."""
    import subprocess

    try:
        import numpy as np
    except ImportError:
        clips, notes = detect_clips_ffmpeg(source, settings, on_progress,
                                           cancelled)
        notes.insert(0, "numpy is not installed - fell back to FFmpeg's scene "
                        "filter, which splits clips on fast camera motion.")
        return clips, notes

    from . import ffmpeg as ff

    notes: list[str] = []
    info = ff.probe(source)
    duration = info.usable_duration
    if duration <= 0:
        raise DetectionError(f"Could not read a duration from {source.name}")

    fps = float(settings.fps or 30)
    expected = max(1, int(duration * fps))
    frame_bytes = _HIST_SIZE * _HIST_SIZE * 3

    # Which grid cell each pixel belongs to, and the flat histogram slot for
    # (cell, channel, bin). One bincount per frame fills the whole thing --
    # calling np.histogram 48 times per frame was far too slow.
    step = _HIST_SIZE // _HIST_GRID
    rows = np.arange(_HIST_SIZE) // step
    cell_of_pixel = (rows[:, None] * _HIST_GRID + rows[None, :]).ravel()
    channel_offset = np.arange(3) * _HIST_BINS
    slot_base = (cell_of_pixel[:, None] * (3 * _HIST_BINS)
                 + channel_offset[None, :])
    cells = _HIST_GRID * _HIST_GRID
    slots = cells * 3 * _HIST_BINS
    shift = 8 - int(_HIST_BINS).bit_length() + 1   # 256 -> _HIST_BINS bins
    per_cell = step * step * 3                     # samples in one cell

    cmd = [
        ff.ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(source), "-an", "-sn", "-dn",
        "-vf", f"fps={fps:g},scale={_HIST_SIZE}:{_HIST_SIZE}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            bufsize=frame_bytes * 8,
                            creationflags=ff._NO_WINDOW)
    hists: list = []
    try:
        assert proc.stdout is not None
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            if cancelled and len(hists) % 32 == 0 and cancelled():
                proc.terminate()
                raise DetectionCancelled("scene detection cancelled")
            pixels = np.frombuffer(raw, np.uint8).reshape(-1, 3)
            counts = np.bincount((slot_base + (pixels >> shift)).ravel(),
                                 minlength=slots)
            hists.append((counts[:slots] / per_cell).astype(np.float32))
            if on_progress and len(hists) % 30 == 0:
                on_progress(min(0.99, len(hists) / expected))
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()

    if on_progress:
        on_progress(1.0)
    if len(hists) < 2:
        raise DetectionError(f"Could not read frames from {source.name}")

    frames = np.asarray(hists)
    n = len(frames)

    def _distance(a, b):
        """Mean per-cell histogram difference, 0 = identical, 1 = nothing shared."""
        diff = np.abs(a - b).reshape(-1, cells, 3 * _HIST_BINS)
        return diff.sum(axis=2).mean(axis=1) / 2.0

    adjacent = _distance(frames[1:], frames[:-1])

    window = max(1, int(_WINDOW * fps))
    gap = max(1, int(_GAP * fps))
    # No `or` fallback here: 0 is a meaningful value meaning "work it out from
    # this video", and `0.0 or x` would quietly substitute x for it.
    strength = float(settings.cut_strength)

    # Window score for every frame, via cumulative sums so it is one pass.
    padded = np.vstack([np.zeros((1, frames.shape[1]), np.float64),
                        np.cumsum(frames, axis=0, dtype=np.float64)])

    def _mean(lo: int, hi: int):
        # padded has n + 1 rows, so a slice end of n is the last valid index.
        lo = max(0, min(n - 1, lo))
        hi = max(lo + 1, min(n, hi))
        return (padded[hi] - padded[lo]) / (hi - lo)

    window_score = np.zeros(n, np.float64)
    for i in range(n):
        before = _mean(i - gap - window, i - gap)
        after = _mean(i + gap, i + gap + window)
        window_score[i] = _distance(before[None, :], after[None, :])[0]

    # The two signals do not peak on the same frame. Whether this is a cut is
    # best judged by the before/after contrast, which peaks a few frames early
    # where nothing is moving yet; where the cut actually falls is the frame the
    # picture jumps on. Reading the position off the contrast peak put cuts up
    # to 0.9s late, which bleeds the end of one scene into the start of the
    # next. So: the contrast decides whether, the jump decides where.
    reach = max(2, int(0.20 * fps))
    min_len = max(0.1, settings.min_scene_len or 1.0)
    if verified:
        # Two of the labelled real cuts sit 0.94s apart; a 1s spacing can
        # only ever keep one of them. With the verifier behind us the close
        # pair is allowed through, and if one of them was noise it comes back
        # merged rather than shipping as a mid-shot reaction.
        min_len = min(min_len, _SPACING_VERIFIED)
    spacing = max(1, int(min_len * fps))

    # Peaks are spaced by the minimum clip length. Spacing them closer (0.4s)
    # was tried on the labelled boundaries, on the theory that a broad peak
    # could swallow a nearby real cut; it caught one fewer real cut, so the
    # theory was wrong and this stays as it is.
    peak_spacing = spacing
    order = np.argsort(-window_score)
    peaks: list[int] = []
    for p in order:
        score = float(window_score[p])
        if score < _CANDIDATE_FLOOR:
            break
        if any(abs(int(p) - q) < peak_spacing for q in peaks):
            continue
        peaks.append(int(p))

    candidates: list[tuple[int, float]] = []
    for p in peaks:
        score = float(window_score[p])
        lo = max(1, p - reach)
        hi = min(n, p + reach + 1)
        local = adjacent[lo - 1:hi - 1]
        if len(local) and local.max() >= _ADJACENT_MIN:
            at = lo + int(np.argmax(local))
        else:
            at = p
        candidates.append((at, score))


    if strength <= 0:
        strength = _auto_strength([c[1] for c in candidates], verified)
        notes.append(
            f"Cut strength {strength:.2f}, chosen from this video's own "
            "score spread"
            + (" (recall profile - boundaries go to the vision check)."
               if verified else ".")
        )

    candidates.sort(key=lambda c: -c[1])
    kept: list[tuple[int, float]] = []
    for index, score in candidates:
        if score < strength:
            continue
        if any(abs(index - other) < spacing for other, _ in kept):
            continue
        kept.append((index, score))

    kept, forced_at = _split_long_runs(kept, candidates, n, fps, settings,
                                       notes)

    kept.sort()
    cuts = [(index / fps, score) for index, score in kept]
    forced_starts = {round(index / fps, 3) for index in forced_at}
    return _clips_from_scored_cuts(source, cuts, duration, settings, notes,
                                   strength, verified=verified,
                                   forced_starts=forced_starts)


def _merge_short_clips(boundaries: list[tuple[float, float]], duration: float,
                       settings: Settings, notes: list[str],
                       verified: bool = False
                       ) -> list[tuple[float, float]]:
    """Absorb too-short clips into a neighbour by dropping the weaker boundary.

    No pixel or colour measure tried here could reliably tell "same shot,
    things moved" from "different shot" - on boundaries the detector itself
    picked, real cuts scored 0.52-0.58 and boundaries that turned out to be
    mid-shot scored 0.49-0.62, fully interleaved. Adjacent-frame difference,
    whole-frame histograms, grid histograms, single-frame jump share and
    motion-compensated alignment all overlapped the same way. It is a semantic
    judgement, which is what the Vision LLM step (req 11) exists for.

    Length, though, is reliable: a mid-shot boundary tends to leave a
    two-second sliver, while real clips in this material run 3-18s. Merging
    slivers away removes most of those boundaries without judging content at
    all, and the boundary dropped is the weaker-scoring of the two around the
    sliver - the one more likely to be spurious.

    This trades the right way round for this tool: losing a real cut merely
    gives one clip two scenes and one reaction, whereas a spurious one drops a
    reaction into the middle of a shot.
    """
    if len(boundaries) < 2:
        return boundaries

    minimum = float(settings.min_clip_duration)
    if minimum < 0:
        return boundaries
    if minimum == 0:
        # Auto, for the same reason the threshold is auto: "too short" depends
        # on the video. Footage cut every two seconds has no slivers at all,
        # and a fixed 2.5s would merge the whole thing away. A fraction of the
        # median clip length scales with whatever arrives.
        lengths = sorted(
            (boundaries[i + 1][0] if i + 1 < len(boundaries) else duration)
            - boundaries[i][0]
            for i in range(len(boundaries))
        )
        median = lengths[len(lengths) // 2]
        fraction = _MERGE_FRACTION_VERIFIED if verified else _MERGE_FRACTION
        floor = _MERGE_MIN_VERIFIED if verified else _MERGE_MIN
        minimum = min(_MERGE_MAX, max(floor, median * fraction))
        notes.append(
            f"Slivers under {minimum:.1f}s merged, from a median clip length "
            f"of {median:.1f}s."
        )
    if minimum <= 0:
        return boundaries

    working = list(boundaries)
    merged = 0
    while len(working) > 1:
        lengths = [
            (working[i + 1][0] if i + 1 < len(working) else duration) - working[i][0]
            for i in range(len(working))
        ]
        shortest = min(range(len(lengths)), key=lambda i: lengths[i])
        if lengths[shortest] >= minimum:
            break

        # Drop the weaker of the boundaries bracketing this sliver. Index 0 is
        # the start of the video and is not a boundary that can be dropped.
        options = []
        if shortest > 0:
            options.append((working[shortest][1], shortest))
        if shortest + 1 < len(working):
            options.append((working[shortest + 1][1], shortest + 1))
        if not options:
            break
        options.sort()
        working.pop(options[0][1])
        merged += 1

    if merged:
        notes.append(
            f"Merged {merged} sliver(s) into a neighbour, so no reaction "
            "lands on a fragment of a shot."
        )
    return working


def _auto_strength(scores: list[float], verified: bool = False) -> float:
    """Pick the cut threshold from this video's own score spread.

    A threshold calibrated on one video is a guess about the next one, and the
    tool has to work on whatever gets dropped into it. The scores in any video
    fall into two groups - moments the picture keeps going through, and moments
    it changes at - so Otsu's method finds the split between them per video
    rather than assuming a number.

    Otsu's split sits a little low for this job, because its lower group mixes
    "nothing happening" with "camera moving", and camera movement is the thing
    that must not be mistaken for a cut. `_AUTO_MARGIN` lifts it clear of that.
    Measured on six videos, Otsu chose 0.39-0.43 and the lifted value 0.45-0.50
    - the same place hand-calibration landed, but now derived per video.
    """
    import numpy as np

    values = np.asarray([s for s in scores if s > 0], dtype=float)
    if len(values) < 6:
        return _AUTO_FALLBACK

    best_variance, split = -1.0, None
    for candidate in np.linspace(0.15, 0.85, 141):
        low = values[values < candidate]
        high = values[values >= candidate]
        if not len(low) or not len(high):
            continue
        weight = len(low) * len(high) / len(values) ** 2
        variance = weight * (low.mean() - high.mean()) ** 2
        if variance > best_variance:
            best_variance, split = variance, float(candidate)

    if split is None:
        return _AUTO_FALLBACK
    if verified:
        return float(min(_AUTO_MAX_VERIFIED,
                         max(_AUTO_MIN_VERIFIED, split * _AUTO_MARGIN_VERIFIED)))
    return float(min(_AUTO_MAX, max(_AUTO_MIN, split * _AUTO_MARGIN)))


def _split_long_runs(kept: list[tuple[int, float]],
                     candidates: list[tuple[int, float]], total_frames: int,
                     fps: float, settings: Settings, notes: list[str]
                     ) -> tuple[list[tuple[int, float]], set[int]]:
    """Break up over-long clips at their strongest inner candidates.

    Two regimes, and the difference matters:

    - `max_clip_duration` set by hand: every stretch longer than it is split.
      Unchanged behaviour.
    - `max_clip_duration` off (0, the default): a stretch is only touched when
      it is longer than the WHOLE target output. A clip like that cannot be
      used at all - the planner used to skip it, or ship it whole with a
      single reaction at the end, which on a 7-minute input produced "one
      reaction at the start, then the rest of the video untouched". A stretch
      that oversized is chopped down to pieces of about a quarter of the
      target (clamped 20-60s), at the most cut-like moments inside it, so the
      output gets a reaction every half-minute or so instead of none.

    Returns the kept cuts plus the frame indices of the splits that were
    forced rather than detected, so downstream can mark them and the vision
    check can leave them alone.
    """
    cap = float(settings.max_clip_duration or 0.0)
    target = float(settings.target_duration or 0.0)
    auto = cap <= 0
    if auto:
        if target <= 0 or not candidates:
            return kept, set()
        cap = min(60.0, max(20.0, target / 4.0))
    if not candidates:
        return kept, set()

    cap_frames = int(cap * fps)
    target_frames = int(target * fps) if target > 0 else 0
    min_frames = max(1, int(max(0.1, settings.min_clip_duration or 1.0) * fps))
    if cap_frames <= min_frames * 2:
        return kept, set()

    # In auto mode only the stretches longer than the whole target are fair
    # game, and splits stay inside them: a legitimate 90s clip in the same
    # video is not chopped just because a 7-minute stretch elsewhere was.
    spans = None
    if auto:
        edges = [0] + sorted(i for i, _ in kept) + [total_frames]
        spans = [(edges[b], edges[b + 1]) for b in range(len(edges) - 1)
                 if edges[b + 1] - edges[b] > target_frames]
        if not spans:
            return kept, set()

    def _in_scope(a: int, b: int) -> bool:
        if spans is None:
            return True
        return any(sa <= a and b <= sb for sa, sb in spans)

    by_index = dict(candidates)
    cuts = sorted(i for i, _ in kept)
    forced: set[int] = set()

    while True:
        boundaries = [0] + cuts + [total_frames]
        stretch = None
        for b in range(len(boundaries) - 1):
            a, z = boundaries[b], boundaries[b + 1]
            if z - a > cap_frames and _in_scope(a, z):
                stretch = (a, z)
                break
        if stretch is None:
            break

        low = stretch[0] + min_frames
        high = stretch[1] - min_frames
        best_index, best_score = None, -1.0
        for index, score in candidates:
            if low <= index <= high and score > best_score:
                best_index, best_score = index, score
        if best_index is None:
            # No candidate anywhere inside: one long featureless take. Cut it
            # in half anyway - in this regime the alternative is a video with
            # no reactions in it, which is the complaint that created this
            # function.
            best_index = (stretch[0] + stretch[1]) // 2
            if not (low <= best_index <= high):
                break
        cuts.append(best_index)
        cuts.sort()
        forced.add(best_index)
        if len(forced) > 200:
            break

    if forced:
        if auto:
            notes.append(
                f"{len(forced)} split(s) forced: a stretch ran longer than "
                f"the {target:g}s target itself, so it was cut into ~{cap:g}s "
                "pieces at its most cut-like moments. Without this the video "
                "would ship with almost no reactions in it."
            )
        else:
            notes.append(
                f"{len(forced)} clip(s) ran longer than "
                f"{settings.max_clip_duration:g}s and were split at their "
                "most cut-like point, so no single stretch plays without a "
                "reaction."
            )

    merged = [(i, by_index.get(i, 1.0)) for i in cuts]
    return merged, forced


def _clips_from_scored_cuts(source: Path, cuts: list[tuple[float, float]],
                            duration: float, settings: Settings,
                            notes: list[str], strength: float = _AUTO_FALLBACK,
                            verified: bool = False,
                            forced_starts: set[float] | None = None
                            ) -> tuple[list[Clip], list[str]]:
    """Build clips from confirmed cuts, carrying each cut's strength as confidence."""
    if not cuts:
        notes.append(
            "No cuts detected - treating the whole video as a single clip. "
            "Lower the cut strength if it really does contain separate clips."
        )
        return [Clip(source=source, start=0.0, end=duration, index=0,
                     confidence=0.5)], notes

    boundaries = _merge_short_clips([(0.0, 1e9)] + cuts, duration, settings,
                                    notes, verified=verified)
    clips: list[Clip] = []
    dropped_short = dropped_long = 0

    for i, (start, score) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else duration
        span = end - start
        if settings.max_clip_duration and span > settings.max_clip_duration:
            # _split_long_runs already tried; it had nothing to split on, so
            # keep the footage and say so rather than discard it.
            dropped_long += 1
        # a bare pass -> 0.5 confidence, well clear of it -> certain.
        confidence = 1.0 if i == 0 else max(
            0.0, min(1.0, 0.5 + (score - strength) / 0.25 * 0.5))
        clips.append(Clip(source=source, start=start, end=end,
                          index=len(clips), confidence=confidence))

    if dropped_long:
        notes.append(f"{dropped_long} clip(s) stayed longer than "
                     f"{settings.max_clip_duration:g}s - no cut-like point was "
                     "found inside them to split on.")
    if not clips:
        raise DetectionError(
            f"{len(boundaries)} clips found in {source.name} but all were "
            "filtered out by the minimum clip duration.")

    # Informational only. This used to decide which boundaries went to the
    # vision check, until an audit of every boundary showed the score does not
    # know which ones are wrong: 2 landed in this band while 7 were mistakes.
    # The verifier now looks at all of them; this line just says how much of
    # the detector's own output it was unsure about.
    doubtful = sum(1 for c in clips if c.confidence < _BORDERLINE)
    if doubtful:
        notes.append(f"{doubtful} of {len(clips)} boundaries scored weakly "
                     f"(under {_BORDERLINE:.2f}); confidence alone does not "
                     "say which are wrong, so every boundary is verified.")
    return clips, notes
