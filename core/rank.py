"""Score clips on how worth reacting to they are (funniest first).

This is the one job in the pipeline that genuinely needs a language model. Cut
detection is a measurement and the planner is arithmetic, but "which of these
27 clips is funniest" is a judgement about content - no pixel statistic gets
near it. So the ranking is the reason the vision model is here; the boundary
check in `vision.py` is a bonus that rides the same client.

Each clip becomes one small filmstrip - three frames from across it, side by
side - and the model scores it. Filmstrips are batched into few requests, so a
27-clip video costs three calls rather than 27.

Like the boundary check, this must never break a render: no key, no package, a
network error, a malformed reply, and the planner falls back to taking clips in
order. A ranked run and an unranked run differ in which clips get chosen, never
in whether a video comes out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import xtro
from .config import Settings
from .models import Clip

StatusCb = Optional[Callable[[str], None]]

# Frames sampled per clip, and the width of each in the filmstrip. Three frames
# across a clip show what happens in it; one frame often misses the moment.
_FRAMES_PER_CLIP = 3
_FRAME_WIDTH = 240
# Clips per request. Requests, not tokens, are the scarce resource on this key
# (500/day against 1,000,000 tokens), so batch wide enough that a whole video
# is normally one call - 21 clips cost ~4.5k tokens, nowhere near the limit.
_BATCH = 30

_SYSTEM = (
    "You rate clips from a compilation video on how well each one works as a "
    "moment to react to.\n\n"
    "Each image is a filmstrip: three frames sampled across one clip, left to "
    "right in time order. Judge the clip, not the image quality.\n\n"
    "Score 0-10, where:\n"
    "  8-10  something striking, funny, surprising or satisfying happens - a "
    "fail, a near miss, an impressive result, a visible payoff\n"
    "  4-7   something happens and it holds attention, but it is ordinary\n"
    "  0-3   nothing much happens: static shots, filler, someone walking, an "
    "unclear or mostly obscured frame\n\n"
    "Rate each clip on its own merits. Do not spread scores out to fill the "
    "range, and do not assume a compilation must contain high scorers - if "
    "every clip is ordinary, score them all in the middle.\n\n"
    'Reply with JSON only, no prose: {"ratings":[{"image":1,"score":7,'
    '"reason":"few words on what happens"}]}'
)

class ClipRating:
    """What the model thought of one clip."""

    __slots__ = ("index", "score", "reason")

    def __init__(self, index: int, score: float, reason: str = ""):
        self.index = index
        self.score = score
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ClipRating(index={self.index}, score={self.score:.1f})"


def available(settings: Settings) -> bool:
    """Whether ranking can run at all: switched on and a key configured."""
    return bool(settings.rank_clips) and xtro.configured()


def affordable(settings: Settings) -> tuple[bool, str]:
    """Whether there is request budget left to spend on ranking.

    The key allows 500 requests a day, and a day of rendering is 50-60 videos,
    so the budget is worth checking before spending rather than discovering it
    mid-batch. Unknown budget is treated as affordable.
    """
    reserve = int(settings.rank_min_requests or 0)
    if reserve <= 0:
        return True, ""
    left = xtro.remaining_requests()
    if left is None:
        return True, ""
    if left < reserve:
        return False, (f"Only {left} XtroEdge requests left today; skipping "
                       f"clip rating and using clip order instead.")
    return True, ""


def rate_clips(clips: Sequence[Clip], source: Path, settings: Settings,
               on_status: StatusCb = None,
               cancelled: Optional[Callable[[], bool]] = None
               ) -> tuple[list[ClipRating], list[str]]:
    """Score every clip. Returns (ratings, notes); ratings is empty on failure.

    An empty list is the signal to fall back - callers must not treat missing
    ratings as "every clip scores zero".
    """
    status = on_status or (lambda _m: None)
    notes: list[str] = []
    clips = list(clips)
    if not clips:
        return [], notes
    if not available(settings):
        return [], notes

    strips: list[tuple[int, bytes]] = []
    for i, clip in enumerate(clips):
        if cancelled and cancelled():
            return [], notes
        image = _filmstrip(source, clip)
        if image:
            strips.append((i, image))
    if not strips:
        notes.append("Could not read frames for clip rating; using clip order.")
        return [], notes

    try:
        ratings = _ask(strips, settings, status, cancelled)
    except Exception as exc:
        notes.append(f"Clip rating skipped ({exc}); using clip order instead.")
        return [], notes

    if not ratings:
        notes.append("Clip rating returned nothing; using clip order instead.")
        return [], notes

    scored = sorted(ratings, key=lambda r: -r.score)
    best = ", ".join(f"clip {r.index + 1} ({r.score:.0f})" for r in scored[:5])
    notes.append(f"Rated {len(ratings)} clips; best: {best}.")
    return ratings, notes


# --------------------------------------------------------------------------
def _ask(strips: list[tuple[int, bytes]], settings: Settings,
         status: Callable, cancelled: Optional[Callable[[], bool]]
         ) -> list[ClipRating]:
    model = settings.vision_model or "XtroEdge Pro v3"
    out: list[ClipRating] = []
    batches = (len(strips) + _BATCH - 1) // _BATCH

    for number, start in enumerate(range(0, len(strips), _BATCH), 1):
        if cancelled and cancelled():
            break
        batch = strips[start:start + _BATCH]
        status(f"Rating clips, batch {number}/{batches}")

        content: list[dict] = []
        for position, (_, image) in enumerate(batch, 1):
            content.append({"type": "text", "text": f"Filmstrip {position}:"})
            content.append(xtro.image_block(image))
        content.append({
            "type": "text",
            "text": (f"Score all {len(batch)} clips, using the filmstrip "
                     f"numbers above. JSON only."),
        })

        reply = xtro.message(content, system=_SYSTEM, model=model,
                             max_tokens=1500)
        for rating in _parse(reply):
            position = rating.get("image")
            score = rating.get("score")
            if not isinstance(position, int) or not 1 <= position <= len(batch):
                continue
            if not isinstance(score, (int, float)):
                continue
            out.append(ClipRating(
                index=batch[position - 1][0],
                score=max(0.0, min(10.0, float(score))),
                reason=str(rating.get("reason") or "")[:120],
            ))
    return out


def _parse(reply: str) -> list[dict]:
    """Ratings out of a reply, tolerating prose or a markdown fence around it."""
    data = xtro.parse_json(reply)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        ratings = data.get("ratings")
        if isinstance(ratings, list):
            return [r for r in ratings if isinstance(r, dict)]
    return []


def _filmstrip(source: Path, clip: Clip) -> Optional[bytes]:
    """Three frames from across one clip, side by side in one JPEG."""
    from . import ffmpeg as ff

    span = clip.duration
    if span <= 0:
        return None
    # Sample inside the clip, away from the boundaries where a cut's own
    # transition frames would otherwise dominate the strip.
    offsets = [span * fraction for fraction in (0.2, 0.5, 0.8)][:_FRAMES_PER_CLIP]

    # -frames:v is an OUTPUT option. Repeating it per input silently yields a
    # 365-byte non-image, which reads downstream as "no frames available".
    cmd = [ff.ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error"]
    for offset in offsets:
        cmd += ["-ss", f"{clip.start + offset:.3f}", "-i", str(source)]
    graph = "".join(f"[{i}:v]scale={_FRAME_WIDTH}:-2[f{i}];"
                    for i in range(len(offsets)))
    graph += "".join(f"[f{i}]" for i in range(len(offsets)))
    graph += f"hstack=inputs={len(offsets)}"
    cmd += ["-filter_complex", graph, "-frames:v", "1", "-q:v", "4",
            "-f", "mjpeg", "-"]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=90,
                              creationflags=ff._NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout or None


def scores_by_index(ratings: Sequence[ClipRating]) -> dict[int, float]:
    """Lookup the planner can use; missing clips simply have no entry."""
    return {r.index: r.score for r in ratings}
