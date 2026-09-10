"""A 7-minute input with no detectable cuts must still plan a full video.

The failure this guards against was reported from a client machine: a 7-minute
input came out with one reaction near the start and the rest of the footage
untouched. Mechanism: the detector found no (or few) cuts, the surviving clip
was longer than the whole 180s target, and a clip that size is unusable - the
planner either skipped it or shipped it whole with a single reaction at its
end. Nothing in the suite rendered anything longer than ~4 minutes, so the
regime never got tested.

The fixture is seven minutes of ffmpeg's `testsrc` - genuinely one continuous
shot, zero hard cuts, which is the hardest version of the problem. The bar:

  - no planned clip may be longer than the target
  - the plan must land on the target length
  - the output must alternate through at least 4 reactions, because the
    complaint was "one reaction, then nothing"

Runs entirely offline (vision and rating off), so it costs no quota.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ffmpeg  # noqa: E402
from core.config import Settings  # noqa: E402
from core.library import ReactionLibrary  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from tests import lib  # noqa: E402

NAME = "long single-shot input"

_FIXTURE = lib.TRUTH_DIR / "longshot_420s.mp4"
_SECONDS = 420


def _fixture() -> Path:
    """Seven minutes of continuously moving synthetic footage, cached."""
    if _FIXTURE.exists() and _FIXTURE.stat().st_size > 100_000:
        return _FIXTURE
    lib.TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=30:duration={_SECONDS}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(_FIXTURE),
    ], check=True, timeout=600)
    return _FIXTURE


def run() -> tuple[bool, list[str]]:
    out: list[str] = []
    library = ReactionLibrary()
    if not library.active_reactions():
        out.append("  [skip] reaction library is empty")
        return True, out

    source = _fixture()
    settings = Settings.load()
    # Offline on purpose: this regime must work on a machine with no key,
    # because that is exactly where it failed.
    settings.vision_enabled = False
    settings.rank_clips = False
    settings.clip_selection = "sequential"

    pipeline = Pipeline(settings, library)
    plan, clips = pipeline.plan_only(source)

    ok = True
    target = settings.target_duration
    longest = max(c.duration for c in clips)
    out.append(f"  {len(clips)} clips from a {_SECONDS}s single shot, "
               f"longest {longest:.1f}s")

    if longest > target:
        out.append(f"  [FAIL] a clip is longer than the {target:g}s target - "
                   "the planner cannot use it")
        ok = False
    else:
        out.append(f"  [ok] every clip fits inside the {target:g}s target")

    drift = abs(plan.total_duration - target)
    if drift > 0.5:
        out.append(f"  [FAIL] plan is {plan.total_duration:.2f}s, "
                   f"{drift:.2f}s from the target")
        ok = False
    else:
        out.append(f"  [ok] plan lands on {plan.total_duration:.2f}s")

    reactions = sum(1 for seg in plan.segments if seg.kind == "reaction")
    if reactions < 4:
        out.append(f"  [FAIL] only {reactions} reaction(s) in the plan - "
                   "this is the '7-minute video, one reaction' bug")
        ok = False
    else:
        out.append(f"  [ok] {reactions} reactions spread through the output")

    for note in plan.notes:
        if "forced" in note:
            out.append(f"    note: {note}")
    return ok, out
