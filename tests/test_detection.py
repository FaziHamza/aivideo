"""Detection, scored three ways - recall on constructed truth, then precision
on hand-labelled boundaries both without and with the vision check.

The thresholds below are the bar this project holds itself to, not aspirations:

  recall            at least 80% of known cuts found on constructed video
  false splits raw  at most 7 of the labelled same-scene boundaries split by
                    the pixel measures alone - the number measured today, kept
                    as a regression guard, not as an acceptable result
  false splits      at most 2 once the vision check has run; this is the
                    shipped configuration and the number that matters
  cuts kept         at least 11 of the 17 labelled cuts survive the check

The false-split bar is the strict one on purpose. A missed cut leaves one clip
holding two scenes and one reaction, which reads as ordinary. A false split
drops a reaction into the middle of a shot, which reads as broken - so the
tolerance for it is near zero and the tolerance for missed cuts is not.

Why two precision numbers rather than one. The raw number is what the machine
can do for free and it is poor: 7 of 24 continuous shots split down the middle.
The vision check takes that to 1 for about two API requests a video. Reporting
only the second would hide how much of the accuracy is bought rather than
computed; reporting only the first would grade a configuration nobody ships.

The vision half spends real quota (two requests per run). Pass --offline to
skip it; the raw half always runs.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import vision, xtro  # noqa: E402
from core.config import Settings  # noqa: E402
from tests import lib  # noqa: E402

NAME = "clip detection"

MIN_RECALL = 0.80
MAX_FALSE_SPLITS_RAW = 7
MAX_FALSE_SPLITS = 2
# Measured 12 of 17 on the labelled video; the floor sits one below because
# the gateway rejects `temperature` so verdicts vary by about one boundary
# between identical runs. A drop to 10 is a real regression, not noise.
MIN_CUTS_SHIPPED = 11


def _recall(out: list[str]) -> bool:
    """Cuts found on video whose every join is a cut by construction."""
    out.append("  constructed ground truth (every join is a known cut):")
    manifest = lib.build_truth()
    caught = total = 0
    for name, spec in sorted(manifest.items()):
        known = spec["boundaries"]
        if not known:
            continue
        started = time.perf_counter()
        clips, _ = lib.detect(spec["path"], min_scene_len=0.8)
        elapsed = time.perf_counter() - started
        found = lib.boundaries_of(clips)
        hits = sum(1 for k in known if lib.near(k, found, 0.5))
        caught += hits
        total += len(known)
        out.append(f"    {name:<7} {elapsed:5.1f}s  {len(clips):3d} clips  "
                   f"recall {hits}/{len(known)}")

    if not total:
        return True
    recall = caught / total
    verdict = "ok" if recall >= MIN_RECALL else "FAIL"
    out.append(f"  [{verdict}] recall {caught}/{total} = {recall*100:.0f}% "
               f"(floor {MIN_RECALL*100:.0f}%)")
    return recall >= MIN_RECALL


def _report(out: list[str], title: str, report: dict) -> None:
    out.append(f"    {title}")
    out.append(f"      real cuts caught : {report['cuts_caught']}/"
               f"{report['cuts_total']}")
    out.append(f"      false splits     : {len(report['false_splits'])}/"
               f"{report['same_total']}")
    if report["false_splits"]:
        out.append("        false at: " +
                   ", ".join(f"{b:.2f}s" for b in report["false_splits"]))


def run() -> tuple[bool, list[str]]:
    out: list[str] = []
    if not lib.have_footage():
        out.append("  [skip] source footage not present")
        return True, out

    ok = _recall(out)

    labels = lib.load_labels()
    video = lib.ROOT / labels["video"]
    if not video.exists():
        out.append("  [skip] labelled video not present")
        return ok, out

    clips, notes = lib.detect(video)
    raw = lib.audit(lib.boundaries_of(clips), labels)
    out.append("")
    out.append(f"  labelled footage ({video.name}), EVERY boundary checked:")
    out.append(f"    {len(clips)} clips, {raw['found']} boundaries")
    _report(out, "pixel measures alone:", raw)
    out.append(f"    not yet labelled : {len(raw['unlabelled'])}")

    if raw["unlabelled"]:
        shown = ", ".join(f"{b:.2f}s" for b in raw["unlabelled"][:10])
        out.append(f"      unlabelled: {shown}"
                   f"{' ...' if len(raw['unlabelled']) > 10 else ''}")
        out.append("      (label these with `cli.py boundaries <video>` - an "
                   "unlabelled boundary is one this test cannot grade)")

    if len(raw["false_splits"]) > MAX_FALSE_SPLITS_RAW:
        out.append(f"  [FAIL] the detector alone now splits "
                   f"{len(raw['false_splits'])} shots, was "
                   f"{MAX_FALSE_SPLITS_RAW}")
        ok = False

    # --- the shipped path: the same boundaries, verified ---
    settings = Settings.load()
    out.append("")
    if "--offline" in sys.argv:
        out.append("  [skip] vision check not run (--offline)")
        return ok, out
    if not settings.vision_enabled:
        # The user can switch the check off in the GUI, and that choice lives
        # in the same settings.json this test loads. Their preference is not
        # a regression - this suite measures what the shipped path CAN do, so
        # force it on for the measurement and say so.
        out.append("  note: this machine currently has the cut check "
                   "switched OFF; measuring with it on anyway")
        settings.vision_enabled = True
    if not xtro.configured():
        out.append("  [skip] vision check needs an XtroEdge key; without one "
                   "the raw numbers above are what this machine produces")
        return ok, out

    # The pipeline detects at the recall profile when the verifier is live,
    # so the shipped measurement has to do the same - grading the conservative
    # boundaries against the verifier would test a combination nobody runs.
    started = time.perf_counter()
    recall_clips, _ = lib.detect(video, verified=True)
    verifier = vision.VisionBoundaryVerifier(settings)
    fixed = verifier.verify(recall_clips, video, settings)
    elapsed = time.perf_counter() - started
    if getattr(verifier, "failed", False):
        # Auditing now would grade unverified recall-profile boundaries -
        # a combination the pipeline itself refuses to ship (it re-detects
        # conservatively instead). Report the miss, don't fake a number.
        out.append("  [skip] the vision call failed mid-check "
                   f"({'; '.join(verifier.notes) or 'no detail'}) - "
                   "re-run when the API is reachable")
        return ok, out
    shipped = lib.audit(lib.boundaries_of(fixed), labels)

    out.append(f"  with the vision check ({elapsed:.0f}s, ~3 requests):")
    out.append(f"    {len(fixed)} clips, {shipped['found']} boundaries")
    _report(out, "shipped configuration:", shipped)

    if len(shipped["false_splits"]) > MAX_FALSE_SPLITS:
        out.append(f"  [FAIL] {len(shipped['false_splits'])} false splits, "
                   f"ceiling is {MAX_FALSE_SPLITS}")
        ok = False
    elif shipped["cuts_caught"] < MIN_CUTS_SHIPPED:
        # Merging every boundary away would score zero false splits, so the
        # ceiling above is only meaningful next to a floor on real cuts.
        out.append(f"  [FAIL] only {shipped['cuts_caught']} real cuts left, "
                   f"floor is {MIN_CUTS_SHIPPED} - the check is merging too "
                   f"much")
        ok = False
    else:
        out.append(f"  [ok] {len(shipped['false_splits'])} false splits and "
                   f"{shipped['cuts_caught']}/{shipped['cuts_total']} cuts "
                   f"kept, both within the bar")

    for note in verifier.notes:
        out.append(f"    note: {note}")
    for note in notes:
        out.append(f"    note: {note}")

    return ok, out
