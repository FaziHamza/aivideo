"""The timeline requirements, checked as arithmetic - no footage needed.

Each check maps to a numbered requirement, so a change that breaks one names
the requirement it broke rather than just failing.
"""

from __future__ import annotations

import random
from dataclasses import replace
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Settings          # noqa: E402
from core.models import Clip, ReactionAsset  # noqa: E402
from core.planner import (PlanningError, build_plan,  # noqa: E402
                          next_rotation_offset)

NAME = "planner requirements"


def _originals(durations, src="in.mp4"):
    out, clock = [], 0.0
    for i, duration in enumerate(durations):
        out.append(Clip(source=Path(src), start=clock, end=clock + duration,
                        index=i))
        clock += duration
    return out


def _reactions(durations):
    return [ReactionAsset(id=i + 1, path=Path(f"r{i+1}.mp4"),
                          label=f"R{i+1}", duration=d)
            for i, d in enumerate(durations)]


def _check(plan, reactions, settings, name, out):
    """Every structural rule the plan must satisfy, whatever the mode."""
    errors = []
    segments = plan.segments

    for i, segment in enumerate(segments):                       # req 4
        expected = "original" if i % 2 == 0 else "reaction"
        if segment.kind != expected:
            errors.append(f"segment {i} is {segment.kind}, expected {expected}")
    if len(segments) % 2:
        errors.append("odd segment count - an original without its reaction")

    starts = [s.clip.start for s in segments if s.kind == "original"]
    if starts != sorted(starts):                                 # req 6
        errors.append(f"originals out of order: {starts}")
    if len(set(starts)) != len(starts):
        errors.append("an original clip was used twice")

    live = [r for r in reactions if r.active]                     # req 5
    expected_rotation = [live[(plan.rotation_offset + i) % len(live)].label
                         for i in range(plan.originals_used)]
    actual_rotation = [s.label for s in segments if s.kind == "reaction"]
    if expected_rotation != actual_rotation:
        errors.append(f"rotation broke: {actual_rotation}")

    trimmed = [i for i, s in enumerate(segments) if s.is_trimmed]  # req 7
    allowed = {len(segments) - 1, len(segments) - 2}
    if trimmed and not set(trimmed).issubset(allowed):
        errors.append(f"trimmed outside the final pair: {trimmed}")

    tail = segments[-1]
    if tail.is_trimmed and tail.out_duration < 0.999:
        errors.append(f"tail shaved to {tail.out_duration:.3f}s, below the 1s floor")

    if (settings.exact_duration and not plan.exhausted and plan.exact_hit
            and abs(plan.total_duration - settings.target_duration) > 0.02):
        errors.append(f"exact mode off target: {plan.total_duration:.3f}s")

    out.append(f"  [{'FAIL' if errors else 'ok'}] {name}: {len(segments)} segs, "
               f"{plan.total_duration:.2f}s (drift {plan.drift:+.2f}), "
               f"{plan.originals_used}/{plan.originals_available} clips")
    out.extend(f"        !! {e}" for e in errors)
    return not errors


def run() -> tuple[bool, list[str]]:
    out: list[str] = []
    ok = True
    random.seed(7)
    settings = Settings()
    settings.clip_selection = "sequential"

    forty = _originals([round(random.uniform(3, 12), 2) for _ in range(40)])
    five = _reactions([2.4, 3.1, 4.7, 5.5, 3.9])
    ok &= _check(build_plan(forty, five, settings), five, settings,
                 "40 clips / 5 reactions", out)

    one = _reactions([3.0])
    ok &= _check(build_plan(forty, one, settings), one, settings,
                 "single reaction repeats in rotation", out)

    six_long = _originals([25.0, 31.5, 40.0, 22.25, 18.5, 29.0])
    ok &= _check(build_plan(six_long, five, settings), five, settings,
                 "six long clips", out)

    tiny = _originals([5.0, 4.0, 6.0])
    plan = build_plan(tiny, five, settings)
    ok &= _check(plan, five, settings, "not enough material", out)
    if not plan.exhausted:
        out.append("        !! short material should be flagged exhausted")
        ok = False

    ok &= _check(build_plan(_originals([200.0]), five, settings), five,
                 settings, "one clip longer than the target", out)

    whole = Settings()
    whole.clip_selection = "sequential"
    whole.exact_duration = False
    plan = build_plan(forty, five, whole)
    ok &= _check(plan, five, whole, "whole-clip mode", out)
    if any(s.is_trimmed for s in plan.segments):
        out.append("        !! whole-clip mode trimmed a segment")
        ok = False

    offset, seen = 0, []
    for _ in range(6):
        plan = build_plan(forty, five, settings, rotation_offset=offset)
        seen.append(plan.segments[1].label)
        offset = next_rotation_offset(plan, len(five))
    if len(set(seen[:5])) != 5:
        out.append(f"        !! rotation did not advance across a batch: {seen}")
        ok = False
    out.append(f"  [ok] batch rotation starts on {seen}")

    big = _originals([round(random.uniform(1.5, 14), 2) for _ in range(300)])
    started = time.perf_counter()
    plan = build_plan(big, five, settings)
    elapsed = (time.perf_counter() - started) * 1000
    ok &= _check(plan, five, settings, f"300 clips ({elapsed:.0f} ms)", out)
    if elapsed > 500:
        out.append(f"        !! planning took {elapsed:.0f} ms")
        ok = False

    try:
        build_plan(forty, [], settings)
        out.append("        !! an empty reaction library was accepted")
        ok = False
    except PlanningError:
        out.append("  [ok] empty reaction library rejected")

    # ratings drive selection, and missing ratings must not read as zero
    rated = Settings()
    rated.clip_selection = "best"
    ratings = {c.index: (9.0 if i % 3 == 0 else 1.0)
               for i, c in enumerate(forty)}
    plan = build_plan(forty, five, rated, ratings=ratings)
    ok &= _check(plan, five, rated, "rated selection", out)
    picked = {s.clip.index for s in plan.segments if s.kind == "original"}
    high = {c.index for i, c in enumerate(forty) if i % 3 == 0}
    if len(picked & high) < len(picked) * 0.6:
        out.append(f"        !! rated selection ignored the ratings: {picked}")
        ok = False
    plan = build_plan(forty, five, rated, ratings=None)
    ok &= _check(plan, five, rated, "rated mode with no ratings (fallback)", out)

    # Every reaction rotation has to land on the target, not just the first
    # one. The rotation advances after each job, so a rotation that cannot
    # reach 3:00 does not fail once - it fails on video 8 of 60, in a batch
    # nobody is watching. One rotation in eleven used to come out 2.5s short
    # because the in-order selector counted only the closing reaction as trim
    # room and therefore preferred a shortfall it could not recover from.
    plenty = _originals([3.27, 3.47, 4.23, 5.1, 5.2, 5.27, 5.73, 5.9, 6.33,
                         6.6, 6.8, 8.8, 10.27, 10.7, 12.03, 13.0, 14.1,
                         16.27, 17.1, 17.6, 20.93])
    eleven = _reactions([1.57, 1.53, 1.57, 1.53, 1.3, 1.63, 0.9, 2.13, 1.7,
                         2.17, 2.07])
    for mode in ("sequential", "fit", "best"):
        rotations = replace(settings, clip_selection=mode)
        misses = []
        for offset in range(len(eleven)):
            plan = build_plan(plenty, eleven, rotations, rotation_offset=offset)
            if abs(plan.total_duration - 180.0) > 0.02:
                misses.append(f"offset {offset} -> {plan.total_duration:.2f}s")
        verdict = "ok" if not misses else "FAIL"
        out.append(f"  [{verdict}] {mode:<10} all {len(eleven)} reaction "
                   f"rotations land on 180s"
                   f"{'' if not misses else ': ' + '; '.join(misses)}")
        ok &= not misses

    worst = 0.0
    for _ in range(40):
        clips = _originals([round(random.uniform(2, 15), 2)
                            for _ in range(random.randint(12, 60))])
        reactions = _reactions([round(random.uniform(2, 7), 2)
                                for _ in range(random.randint(1, 8))])
        plan = build_plan(clips, reactions, settings)
        if not plan.exhausted and plan.exact_hit:
            worst = max(worst, abs(plan.total_duration - 180.0))
    out.append(f"  [{'ok' if worst <= 0.02 else 'FAIL'}] 40-trial sweep: "
               f"worst drift {worst * 1000:.1f} ms")
    ok &= worst <= 0.02

    return ok, out
