"""Shared test helpers: constructed ground truth and boundary auditing.

Two kinds of ground truth, because neither is sufficient alone.

**Constructed** - known segments cut out of the footage and glued back
together, so every join is a real cut at a known timestamp and there are no
others. Recall is then countable rather than judged. This is what catches a
detector that stops finding cuts.

**Labelled** - `labels.json`, boundaries in the real footage judged by eye from
full-size frames. This is what catches a detector that finds cuts that are not
there, which constructed truth cannot measure (a segment carved out of real
footage may contain its own internal cut).

The audit deliberately checks EVERY boundary the detector produces, not a
sample. An earlier round of this work verified only the boundaries that
happened to be labelled while the detector produced roughly twice as many, so
half of what it decided went unexamined - and the unexamined half was where the
bug was.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import ffmpeg, scenes  # noqa: E402
from core.config import Settings  # noqa: E402

TRUTH_DIR = ROOT / "data" / "temp" / "truth"
LABELS = Path(__file__).resolve().parent / "labels.json"
FPS = 30

# (name, [(source key, start seconds, seconds), ...]) - A and B are the two
# long inputs, alternated so consecutive segments are different scenes.
VARIANTS = {
    "even": [("A", 5, 6), ("B", 60, 6), ("A", 70, 6), ("B", 120, 6),
             ("A", 100, 6), ("B", 200, 6), ("A", 155, 6), ("B", 30, 6),
             ("A", 175, 6), ("B", 90, 6)],
    "mixed": [("A", 12, 3), ("B", 45, 9), ("A", 88, 2), ("B", 150, 7),
              ("A", 35, 4), ("B", 15, 12), ("A", 120, 3), ("B", 240, 5),
              ("A", 60, 8), ("B", 75, 2)],
    "long": [("A", 0, 18), ("B", 100, 22), ("A", 60, 15), ("B", 180, 25),
             ("A", 130, 20)],
    "short": [("A", 20, 2), ("B", 20, 2), ("A", 40, 2), ("B", 40, 2),
              ("A", 80, 2), ("B", 80, 2), ("A", 110, 2), ("B", 110, 2),
              ("A", 140, 2), ("B", 140, 2), ("A", 160, 2), ("B", 160, 2),
              ("A", 180, 2), ("B", 60, 2)],
}

SOURCES = {
    "A": ROOT / "resources" / "3 min Clips" / "mainvideo.mp4",
    "B": ROOT / "resources" / "3 min Clips" / "0902(1).mp4",
}


def have_footage() -> bool:
    return all(p.exists() for p in SOURCES.values())


def load_labels() -> dict:
    return json.loads(LABELS.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
def build_truth(rebuild: bool = False) -> dict:
    """Build (or reuse) the constructed-truth videos. Returns a manifest."""
    manifest_path = TRUTH_DIR / "manifest.json"
    if manifest_path.exists() and not rebuild:
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict = {}

    for name, spec in VARIANTS.items():
        parts, boundaries, clock = [], [], 0.0
        for i, (key, start, seconds) in enumerate(spec):
            frames = int(round(seconds * FPS))
            part = TRUTH_DIR / f"{name}_{i:02d}.mp4"
            subprocess.run([
                ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                "-y", "-ss", str(start), "-i", str(SOURCES[key]),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-t", f"{seconds + 1}",
                "-vf", f"fps={FPS},scale=640:360,setsar=1,format=yuv420p",
                "-frames:v", str(frames),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
                "-video_track_timescale", "30000", str(part),
            ], capture_output=True)
            if not part.exists():
                continue
            parts.append(part)
            if i:
                boundaries.append(round(clock, 4))
            clock += frames / FPS

        listing = TRUTH_DIR / f"{name}.txt"
        listing.write_text("\n".join(f"file '{p.resolve().as_posix()}'"
                                     for p in parts) + "\n", encoding="utf-8")
        video = TRUTH_DIR / f"{name}.mp4"
        subprocess.run([
            ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", "-movflags", "+faststart", str(video),
        ], capture_output=True)

        manifest[name] = {
            "path": str(video),
            "boundaries": boundaries,
            "clips": len(parts),
        }

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------
def detect(video: Path, verified: bool = False, **overrides):
    """Detect clips with the shipped settings, plus any overrides.

    `verified=True` selects the recall profile - only meaningful when the
    caller then runs the vision check over the result, as the pipeline does.
    """
    settings = Settings.load()
    for field, value in overrides.items():
        setattr(settings, field, value)
    return scenes.detect_clips(Path(video), settings, verified=verified)


def boundaries_of(clips) -> list[float]:
    return [c.start for c in clips[1:]]


def near(value: float, values, tolerance: float) -> bool:
    return any(abs(value - v) <= tolerance for v in values)


def audit(found: list[float], labels: dict) -> dict:
    """Score detected boundaries against the labelled set.

    Every detected boundary is placed in one of three buckets, so nothing the
    detector decided goes unexamined:

      matched_cut  a labelled real cut
      false_split  a labelled same-scene boundary - a reaction would land
                   mid-shot here
      unlabelled   not in labels.json either way; add a label for it
    """
    tolerance = float(labels.get("tolerance", 0.5))
    cuts = [entry["at"] for entry in labels["cuts"]]
    same = [entry["at"] for entry in labels["same_scene"]]

    matched, false_splits, unlabelled = [], [], []
    for boundary in found:
        if near(boundary, cuts, tolerance):
            matched.append(boundary)
        elif near(boundary, same, tolerance):
            false_splits.append(boundary)
        else:
            unlabelled.append(boundary)

    missed = [c for c in cuts if not near(c, found, tolerance)]
    return {
        "found": len(found),
        "cuts_total": len(cuts),
        "cuts_caught": len(cuts) - len(missed),
        "missed": missed,
        "false_splits": false_splits,
        "same_total": len(same),
        "unlabelled": unlabelled,
        "matched": matched,
    }
