"""Renders one video and checks the file against the plan, frame by frame.

The specific thing this guards: the encoder can only emit whole frames, so a
plan measured in seconds and a file measured in frames drift apart. An earlier
build lost a quarter of a second that way across twenty segments, silently.
This asserts planned frames == actual frames, which is the only form of "the
output is 3 minutes" that cannot quietly be false.

Skipped when the reaction library is empty or the footage is absent, so the
suite still runs on a fresh checkout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ffmpeg, validate  # noqa: E402
from core.config import Settings, TEMP_DIR  # noqa: E402
from core.library import ReactionLibrary  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from tests import lib  # noqa: E402

NAME = "render and validate"


def _frame_count(path: Path) -> int:
    """Exact frame count, not the container's duration guess."""
    result = subprocess.run([
        ffmpeg.ffprobe_path(), "-v", "error", "-select_streams", "v:0",
        "-count_frames", "-show_entries", "stream=nb_read_frames",
        "-of", "csv=p=0", str(path),
    ], capture_output=True, text=True)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return -1


def run() -> tuple[bool, list[str]]:
    out: list[str] = []
    source = lib.SOURCES["A"]
    if not source.exists():
        out.append("  [skip] source footage not present")
        return True, out

    library = ReactionLibrary()
    if not library.active_reactions():
        out.append("  [skip] reaction library is empty")
        return True, out

    settings = Settings.load()
    # Keep the check deterministic and free: clip order, no model call.
    settings.clip_selection = "sequential"
    settings.rank_clips = False
    settings.vision_enabled = False
    # Pin the rotation. Left alone it advances after every job, so this test
    # rendered a different timeline each run - it passed alone and failed
    # inside the full suite, which reads as a render bug and is not one. The
    # rotations themselves are covered by test_planner, cheaply and for all
    # eleven of them.
    settings.rotation_offset = 0
    settings.rotation_advances_per_job = False

    target = TEMP_DIR / "test_render.mp4"
    result = Pipeline(settings, library).process_one(source, output=target)

    if not result.ok or result.plan is None:
        out.append(f"  [FAIL] render failed: {result.error}")
        return False, out

    plan = result.plan
    planned = round(plan.total_duration * settings.fps)
    actual = _frame_count(target)
    info = ffmpeg.probe(target)
    report = validate.validate_output(target, settings, plan)

    ok = True
    out.append(f"  {plan.originals_used} clips + {plan.originals_used} "
               f"reactions, {len(plan.segments)} segments, "
               f"{result.elapsed:.0f}s to render")
    out.append(f"  {info.width}x{info.height}, {info.size_bytes/1048576:.1f} MB")

    verdict = "ok" if planned == actual else "FAIL"
    out.append(f"  [{verdict}] planned {planned} frames, wrote {actual} "
               f"({planned/settings.fps:.4f}s planned)")
    if planned != actual:
        ok = False

    if (info.width, info.height) != (settings.width, settings.height):
        out.append(f"  [FAIL] wrong resolution: {info.width}x{info.height}")
        ok = False
    if not info.has_audio:
        out.append("  [FAIL] no audio track")
        ok = False

    if report.ok:
        out.append("  [ok] validation PASS")
    else:
        out.append(f"  [FAIL] validation: {'; '.join(report.problems)}")
        ok = False

    try:
        target.unlink()
    except OSError:
        pass
    return ok, out
