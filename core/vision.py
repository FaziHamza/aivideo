"""Vision boundary verification (req 11) - the permanent fix for false splits.

Why this exists rather than another threshold. Five cheap measures were tried
for telling "same shot, things moved" from "different shot": adjacent-frame
difference, whole-frame colour histograms, a 4x4 grid of histograms, how much
of the change lands in a single frame, and motion-compensated alignment. On
boundaries the detector itself picked, every one of them overlapped - real cuts
scored 0.52-0.58 while boundaries that turned out to be mid-shot scored
0.49-0.62, fully interleaved. No threshold separates those, so the detector
ships with a length rule that removes most spurious boundaries without judging
content (see scenes._merge_short_clips) and leaves the rest here.

Deciding whether two moments belong to the same shot is a semantic judgement,
which is what a vision model is for. This module renders a six-frame filmstrip
of the second around every boundary and asks one question per strip: does the
picture switch shots somewhere in here, or is it one continuous piece of
motion? Boundaries judged continuous are merged away.

The detector runs at a deliberately trigger-happy profile when this check is
live (see scenes.py, the `verified` flag): it proposes ~40 boundaries on the
test footage where the conservative profile proposes 20, catches 16 of the 17
labelled cuts instead of 13, and leaves the over-splitting for this module to
clean up. Measured end to end on the labelled video: 12 of 17 real cuts kept,
1-2 of 24 continuous shots split (down from 7 with no verifier). The verdicts
vary by about one boundary between runs - the gateway rejects `temperature`,
so they cannot be pinned.

Every boundary, not the doubtful ones. Sending only low-confidence boundaries
was the first design and it missed the point: on the labelled video the score
put 2 of 21 boundaries in the doubtful band while 7 were wrong. The score does
not know which ones it got wrong, so there is nothing to select on. Twenty
boundaries batch into two requests, which is what selecting a third of them
would have cost anyway.


Design constraints this respects:

  - The pipeline never fails because of it. No API key, no package, an HTTP
    error, a malformed reply: every path returns the clips unchanged.
  - Cost is bounded. Frames are paired into one image, batched, and capped per
    video by `vision_max_checks`.
  - The prompt is the accuracy knob. An earlier wording called a different
    camera setup a cut, which is exactly what a half-second pan looks like -
    it left 3 false splits standing. Change the wording, re-run
    `tests.run_all detection`, and read the number.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence

from . import xtro
from .config import Settings
from .models import Clip

# Four samples across one second of footage: 0.5s before the boundary, just
# before it, just after it, 0.5s after it. Two frames 0.5s apart were tried
# first and could not carry the one piece of evidence that separates a cut
# from a camera move: whether the change is INSTANT. With only the outer pair,
# a real cut to a wider view of the same subject looks identical to a fast
# pan - and the model, told that pans are "same", merged four real cuts away.
# The inner pair puts the discontinuity itself in the picture: a cut switches
# abruptly between frames 2 and 3, a pan changes gradually across all four.
# Evenly spaced, not clustered at the boundary: the detector's cut position
# can be off by a frame or three, and with a tight inner pair (0.1s) a cut
# whose true position missed the pair photographed as smooth - four real cuts
# were merged away that way. Six uniform steps cover the whole second, so the
# switch lands between SOME adjacent pair wherever it actually is.
# Widening the span to +/-0.7s at 150px was tried for position error and made
# things worse - the smaller frames hid the abruptness that is the whole
# signal. Position error is fixed at the source instead: the detector snaps a
# verified-mode boundary to the strongest adjacent-frame jump nearby, so the
# switch sits in the middle steps of this strip.
_FRAME_OFFSETS = (-0.5, -0.3, -0.1, 0.1, 0.3, 0.5)
# Per-frame width. Six at 180 costs about what two at 340 did.
_FRAME_WIDTH = 180
# Boundaries per request. Batching amortises the prompt over many strips.
_BATCH = 16

_SYSTEM = (
    "You decide whether one second of video contains a hard cut. Each image "
    "is a filmstrip of SIX frames from a single video, left to right in time "
    "order, 0.2 seconds apart.\n\n"
    "A hard cut is an instant switch to a different shot. Across a strip it "
    "looks like this: up to some point the frames continue one shot, then "
    "between two adjacent frames the view is suddenly a different shot - "
    "different framing, camera position or place - and the remaining frames "
    "continue THAT shot. Cutting from a close-up to a wide view of the same "
    "subject or place is still a cut.\n\n"
    "Answer 'same' when the strip is one continuous shot all the way "
    "through. That includes: a camera that pans, swings, zooms or shakes "
    "(the framing changes smoothly, step by step); and action inside the "
    "shot (something falls, crashes, enters or leaves the frame) while the "
    "camera's view itself carries on. However different the first and last "
    "frames look, if each step follows from the one before as camera motion "
    "or on-screen action, it is one shot.\n\n"
    "Answer 'cut' only when some adjacent pair shows a switch that camera "
    "motion or action cannot explain: the new frame does not continue the "
    "previous one, it replaces it.\n\n"
    "When you genuinely cannot tell, answer 'same' - leaving a cut undetected "
    "joins two scenes into one clip, while inventing one puts a reaction in "
    "the middle of a shot, which is the worse outcome.\n\n"
    'Reply with JSON only, no prose: {"verdicts":[{"image":1,"verdict":"cut"}]}'
)


class BoundaryVerifier(Protocol):
    """Reviews candidate cut points and returns a corrected clip list."""

    def verify(self, clips: Sequence[Clip], source: Path,
               settings: Settings) -> list[Clip]:
        ...


class NoopVerifier:
    """Trust the detector, change nothing."""

    name = "disabled"

    def verify(self, clips: Sequence[Clip], source: Path,
               settings: Settings) -> list[Clip]:
        return list(clips)


@dataclass
class _Candidate:
    index: int          # index into the clip list; this clip's start is the cut
    at: float           # seconds
    image: bytes        # side-by-side JPEG


def get_verifier(settings: Settings) -> BoundaryVerifier:
    """The verifier to use for this run.

    Returns NoopVerifier whenever verification cannot run - switched off or no
    key configured - so enabling the setting on a machine that is not set up
    degrades to the detector's own answer instead of failing a batch.
    """
    if not settings.vision_enabled:
        return NoopVerifier()
    if not xtro.configured():
        return NoopVerifier()
    return VisionBoundaryVerifier(settings)


class VisionBoundaryVerifier:
    """Asks the vision model whether each uncertain boundary is a real cut."""

    name = "xtroedge"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.checked = 0
        self.merged = 0
        # True after a verify() in which the model was never reached. The
        # pipeline reads this: when detection ran at the recall profile on the
        # promise of verification, an unverified result must not ship.
        self.failed = False
        self.notes: list[str] = []

    # ------------------------------------------------------------------
    def verify(self, clips: Sequence[Clip], source: Path,
               settings: Settings) -> list[Clip]:
        clips = list(clips)
        if len(clips) < 2:
            return clips

        candidates = self._collect(clips, source, settings)
        if not candidates:
            return clips

        try:
            same_shot = self._ask(candidates, settings)
        except Exception as exc:
            # Never fail a render over this check - but do tell the pipeline,
            # which may have detected at higher recall on the promise that
            # these boundaries would be reviewed.
            self.failed = True
            self.notes.append(f"Vision check skipped: {exc}")
            return clips

        self.checked = len(candidates)
        if not same_shot:
            return clips

        merged = _merge_at(clips, same_shot)
        self.merged = len(same_shot)
        self.notes.append(
            f"Vision check: {len(candidates)} boundaries reviewed, "
            f"{len(same_shot)} were mid-shot and merged away, so no reaction "
            f"lands inside those shots."
        )
        return merged

    # ------------------------------------------------------------------
    def _collect(self, clips: list[Clip], source: Path,
                 settings: Settings) -> list[_Candidate]:
        """Frame pairs for the boundaries worth spending a request on."""
        floor = float(settings.vision_confidence_floor)
        # Skip boundaries that were forced to break up an over-long stretch:
        # they are deliberately mid-shot, and the model would (correctly)
        # judge them continuous and undo the only usable structure the video
        # has.
        wanted = [i for i in range(1, len(clips))
                  if clips[i].confidence < floor
                  and not getattr(clips[i], "forced", False)]
        cap = int(settings.vision_max_checks or 0)
        if cap > 0 and len(wanted) > cap:
            # Least confident first, so the cap spends on the worst boundaries.
            wanted = sorted(wanted, key=lambda i: clips[i].confidence)[:cap]
            wanted.sort()

        out: list[_Candidate] = []
        for i in wanted:
            image = _frame_strip(source, clips[i].start)
            if image:
                out.append(_Candidate(index=i, at=clips[i].start, image=image))
        return out

    # ------------------------------------------------------------------
    def _ask(self, candidates: list[_Candidate],
             settings: Settings) -> set[int]:
        """Clip indices whose boundary the model says is mid-shot."""
        model = settings.vision_model or "XtroEdge Pro v3"
        same: set[int] = set()

        for start in range(0, len(candidates), _BATCH):
            batch = candidates[start:start + _BATCH]
            content: list[dict] = []
            for position, candidate in enumerate(batch, 1):
                content.append({"type": "text", "text": f"Image {position}:"})
                content.append(xtro.image_block(candidate.image))
            content.append({
                "type": "text",
                "text": (f"Judge all {len(batch)} images. One verdict per "
                         f"image, using the numbers above. JSON only."),
            })

            reply = xtro.message(content, system=_SYSTEM, model=model,
                                 max_tokens=1500)
            for verdict in _parse(reply):
                position = verdict.get("image")
                if not isinstance(position, int) or not 1 <= position <= len(batch):
                    continue
                if verdict.get("verdict") == "same":
                    same.add(batch[position - 1].index)

        return same


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _parse(reply: str) -> list[dict]:
    """Verdicts out of a reply, tolerating prose or a fence around the JSON."""
    data = xtro.parse_json(reply)
    if isinstance(data, list):
        return [v for v in data if isinstance(v, dict)]
    if isinstance(data, dict):
        verdicts = data.get("verdicts")
        if isinstance(verdicts, list):
            return [v for v in verdicts if isinstance(v, dict)]
    return []


def _frame_strip(source: Path, when: float) -> Optional[bytes]:
    """One JPEG: a filmstrip spanning ~1.4s around `when`, in time order.

    See the note on _FRAME_OFFSETS for why six and why evenly spaced - the
    short version is that two frames cannot show whether the change was
    instant, instant is what "cut" means, and the exact instant is only known
    to within a few frames.
    """
    from . import ffmpeg as ff

    times = [max(0.0, when + offset) for offset in _FRAME_OFFSETS]
    count = len(times)
    # -frames:v is an OUTPUT option; repeating it per input produces no image.
    cmd = [ff.ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error"]
    for at in times:
        cmd += ["-ss", f"{at:.3f}", "-i", str(source)]
    labels = "".join(f"[{i}:v]scale={_FRAME_WIDTH}:-2[f{i}];"
                     for i in range(count))
    chain = "".join(f"[f{i}]" for i in range(count))
    cmd += [
        "-filter_complex", f"{labels}{chain}hstack=inputs={count}",
        "-frames:v", "1", "-q:v", "4", "-f", "mjpeg", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60,
                              creationflags=ff._NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout or None


def _merge_at(clips: list[Clip], drop: set[int]) -> list[Clip]:
    """Join each clip in `drop` onto the clip before it."""
    out: list[Clip] = []
    for i, clip in enumerate(clips):
        if i in drop and out:
            previous = out[-1]
            out[-1] = Clip(source=previous.source, start=previous.start,
                           end=clip.end, index=previous.index,
                           confidence=previous.confidence,
                           forced=previous.forced)
            continue
        out.append(Clip(source=clip.source, start=clip.start, end=clip.end,
                        index=len(out), confidence=clip.confidence,
                        forced=clip.forced))
    return out


def doubtful_clips(clips: Sequence[Clip], settings: Settings) -> list[Clip]:
    """Boundaries a verifier would be asked about."""
    floor = settings.vision_confidence_floor
    return [c for c in clips if c.confidence < floor]


def extract_boundary_frames(source: Path, clips: Sequence[Clip],
                            out_dir: Path, settings: Settings) -> list[Path]:
    """Write the frame pairs to disk, for eyeballing what the model was sent."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for clip in clips:
        if clip.confidence >= settings.vision_confidence_floor:
            continue
        image = _frame_strip(source, clip.start)
        if not image:
            continue
        target = out_dir / f"boundary_{clip.start:08.2f}.jpg"
        target.write_bytes(image)
        written.append(target)
    return written
