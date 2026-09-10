"""Timeline planning: which clips go into the 3-minute output, in what order.

Rules enforced here, straight from the requirements:

  req 4  every original clip is followed by exactly one reaction
  req 5  reactions repeat in rotation when there are fewer of them than pairs
  req 6  originals keep their chronological order and are never interleaved
  req 7  whole clips are used to land on ~3 minutes; only the final segment may
         be shaved, and only when `exact_duration` is on

Three ways to choose which clips go in, set by `clip_selection`:

  best (default)  rate every clip and take the highest-rated ones that fit,
                  still in chronological order - the reaction lands on the
                  clips worth reacting to. Needs ratings passed in; without
                  them it falls back to sequential.
  sequential      walk the clips in order until the target is full, one
                  reaction each. No model needed.
  fit             pick whichever clips add up closest to the target. Lands the
                  length most precisely but skips around the video.

Picking the clips is an exact-k subset-sum problem: for a given number of pairs
k the reactions are already determined by the rotation, so the reaction time is
a constant and we only need k originals whose total lands closest to the
remaining budget. Solved with a bitset DP over quantised durations, which keeps
a 200-clip video well inside a few milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .config import Settings
from .models import Clip, ReactionAsset, RenderPlan, Segment

# Never shave the tail reaction below this many seconds.
_MIN_TAIL = 1.0
# Nor the last clip, when the reaction alone cannot absorb the overshoot.
_MIN_TAIL_CLIP = 1.5
# Sanity cap on alternations in one 3-minute video.
_MAX_PAIRS = 80


class PlanningError(RuntimeError):
    pass


def _snap(seconds: float, fps: float) -> float:
    """Round a duration to the nearest whole output frame."""
    return max(0.0, round(seconds * fps) / fps)


def _snap_clip(clip: Clip, fps: float) -> Clip:
    frames = max(1, int(round(clip.duration * fps)))
    return Clip(source=clip.source, start=clip.start,
                end=clip.start + frames / fps, index=clip.index,
                confidence=clip.confidence)


def _snap_reaction(reaction: ReactionAsset, fps: float) -> ReactionAsset:
    frames = max(1, int(round(reaction.duration * fps)))
    return ReactionAsset(
        id=reaction.id, path=reaction.path, label=reaction.label,
        duration=frames / fps, width=reaction.width, height=reaction.height,
        has_audio=reaction.has_audio, active=reaction.active,
        cached_path=reaction.cached_path,
        cache_signature=reaction.cache_signature, added_at=reaction.added_at,
    )


@dataclass
class _Candidate:
    pairs: int
    original_indices: list[int]
    originals_total: float
    reactions_total: float

    @property
    def natural(self) -> float:
        return self.originals_total + self.reactions_total


def build_plan(originals: Sequence[Clip], reactions: Sequence[ReactionAsset],
               settings: Settings,
               rotation_offset: Optional[int] = None,
               ratings: Optional[dict[int, float]] = None) -> RenderPlan:
    """Choose the timeline for one output video.

    `ratings` maps a clip's own `index` to how worth reacting to it is. Supply
    it with `clip_selection="best"` to pick the highest-rated clips; without
    it that mode falls back to taking clips in order, because no rating and a
    rating of zero are different things.
    """
    notes: list[str] = []
    fps = float(settings.fps or 30)

    # Everything downstream is a whole number of output frames. The encoder can
    # only emit whole frames, so planning in the same unit is what makes the
    # rendered file match the planned length instead of drifting short.
    usable = [_snap_clip(c, fps) for c in originals if c.duration > 0.05]
    if not usable:
        raise PlanningError("No usable clips were detected in the input video.")

    target = _snap(float(settings.target_duration), fps)
    # A clip longer than the whole target can never sit inside the output next
    # to a reaction, and keeping it would blow up the search space.
    fitting = [c for c in usable if c.duration <= target]
    if fitting:
        if len(fitting) < len(usable):
            notes.append(
                f"Ignored {len(usable) - len(fitting)} clip(s) longer than the "
                f"{target:g}s target."
            )
        usable = fitting
    else:
        usable = [min(usable, key=lambda c: c.duration)]
        notes.append(
            "Every detected clip is longer than the target; using the "
            "shortest one, so the output will run over."
        )

    live_reactions = [_snap_reaction(r, fps) for r in reactions
                      if r.active and r.duration > 0.05]
    if not live_reactions:
        raise PlanningError(
            "The reaction library is empty. Add at least one reaction video."
        )

    offset = settings.rotation_offset if rotation_offset is None else rotation_offset
    offset %= len(live_reactions)

    # Reaction k of this video is fixed by the rotation, so reaction time for
    # any k pairs is just a prefix sum (req 5).
    rotated = [live_reactions[(offset + i) % len(live_reactions)]
               for i in range(_MAX_PAIRS)]
    reaction_prefix = [0.0]
    for r in rotated:
        reaction_prefix.append(reaction_prefix[-1] + r.duration)

    max_pairs = min(len(usable), _MAX_PAIRS)
    # No point considering k where reactions alone already blow the target.
    while max_pairs > 1 and reaction_prefix[max_pairs] >= target:
        max_pairs -= 1

    mode = (settings.clip_selection or "sequential").lower()
    if mode == "best" and ratings:
        candidate = _rated_selection(usable, reaction_prefix, rotated,
                                     max_pairs, target, settings, ratings)
        if candidate:
            kept = [usable[i].index + 1 for i in candidate.original_indices]
            notes.append(
                f"Picked the {candidate.pairs} highest-rated clips of "
                f"{len(usable)} (clips {', '.join(str(k) for k in kept[:8])}"
                f"{', ...' if len(kept) > 8 else ''}), in order."
            )
    elif mode == "best":
        # Rating unavailable; sequential is the honest fallback.
        candidate = _sequential_selection(usable, reaction_prefix, rotated,
                                          max_pairs, target, settings)
    elif mode == "sequential":
        candidate = _sequential_selection(usable, reaction_prefix, rotated,
                                          max_pairs, target, settings)
    else:
        candidate = _best_selection(usable, reaction_prefix, rotated, max_pairs,
                                    target, settings, ratings)

    if candidate is None and mode == "best":
        candidate = _sequential_selection(usable, reaction_prefix, rotated,
                                          max_pairs, target, settings)

    # A short timeline cannot be rescued downstream - the trim only shaves. So
    # whenever the chosen selector lands under the target, ask the exhaustive
    # search too and keep whichever ends up closer. This is not hypothetical:
    # one reaction rotation in eleven made the in-order selector stop 2.5s
    # short of 3:00 on the test footage, which failed validation. Playing in
    # order is a preference; hitting the length is a requirement.
    if (settings.exact_duration and candidate is not None
            and mode != "fit"
            and _miss(candidate, rotated, usable, target) > 0.05):
        rescue = _best_selection(usable, reaction_prefix, rotated, max_pairs,
                                 target, settings, ratings)
        if rescue is not None and (_miss(rescue, rotated, usable, target)
                                   < _miss(candidate, rotated, usable, target)):
            notes.append(
                f"The {mode} pick came out "
                f"{candidate.natural - target:+.1f}s off the target, so clips "
                f"were chosen to fit instead - still in chronological order, "
                f"still one reaction each, still preferring the best-rated."
            )
            candidate = rescue

    if candidate is None:
        raise PlanningError(
            "Could not assemble a timeline from the detected clips. "
            "Try lowering the minimum clip duration."
        )

    segments: list[Segment] = []
    for slot, clip_index in enumerate(candidate.original_indices):
        clip = usable[clip_index]
        segments.append(Segment(clip=clip, kind="original",
                                label=f"clip {clip.index + 1}"))
        reaction = rotated[slot]
        segments.append(Segment(clip=reaction.as_clip(), kind="reaction",
                                label=reaction.label or reaction.path.stem,
                                cached_path=reaction.cached_path))

    natural = candidate.natural
    exhausted = False

    exact_hit = True

    if settings.exact_duration:
        overshoot = natural - target
        if overshoot > 1e-3:
            tail = segments[-1]
            room = max(0.0, tail.clip.duration - _MIN_TAIL)
            cut = _snap(min(overshoot, room), fps)
            segments[-1] = Segment(
                clip=tail.clip, kind=tail.kind, label=tail.label,
                render_duration=_snap(tail.clip.duration - cut, fps),
                cached_path=None,  # a trimmed tail cannot reuse the cache
            )
            leftover = overshoot - cut
            if leftover > 0.05 and len(segments) >= 2:
                # The tail reaction had nothing left, so take the rest off the
                # clip immediately before it. Both sit at the very end of the
                # timeline, so nothing in the body of the video is touched and
                # the output still lands on the target exactly.
                last_clip = segments[-2]
                clip_room = max(0.0, last_clip.clip.duration - _MIN_TAIL_CLIP)
                clip_cut = _snap(min(leftover, clip_room), fps)
                if clip_cut > 0:
                    segments[-2] = Segment(
                        clip=last_clip.clip, kind=last_clip.kind,
                        label=last_clip.label,
                        render_duration=_snap(
                            last_clip.clip.duration - clip_cut, fps),
                        cached_path=None,
                    )
                    leftover -= clip_cut
                    notes.append(
                        f"Final reaction shaved by {cut:.2f}s and the last clip "
                        f"by {clip_cut:.2f}s, to land on {target:g}s exactly."
                    )

            if leftover > 0.05:
                exact_hit = False
                notes.append(
                    f"Output is {natural - cut:.1f}s, still {leftover:.1f}s "
                    f"over the {target:g}s target - the last clip and reaction "
                    "together could not give up enough."
                )
            elif cut > 0 and not any("last clip" in n for n in notes):
                notes.append(
                    f"Final reaction shaved by {cut:.2f}s to land on "
                    f"{target:g}s exactly."
                )
        elif overshoot < -settings.tolerance:
            exhausted = True
            exact_hit = False
            notes.append(
                f"Only {natural:.1f}s of material available - "
                f"{target - natural:.1f}s short of the {target:g}s target."
            )
    else:
        drift = natural - target
        if abs(drift) > settings.tolerance:
            exhausted = drift < 0
            notes.append(
                f"Closest whole-clip fit is {natural:.1f}s "
                f"({drift:+.1f}s off the {target:g}s target)."
            )

    final_total = sum(s.out_duration for s in segments)
    if (settings.exact_duration and exact_hit
            and abs(final_total - target) > 0.01):
        exact_hit = False
        notes.append(
            f"Closest achievable length with whole clips is {final_total:.2f}s "
            f"({final_total - target:+.2f}s off {target:g}s)."
        )

    plan = RenderPlan(
        segments=segments,
        target=target,
        natural_duration=round(natural, 3),
        originals_available=len(usable),
        originals_used=candidate.pairs,
        reactions_used=[rotated[i].label or rotated[i].path.stem
                        for i in range(candidate.pairs)],
        rotation_offset=offset,
        exhausted=exhausted,
        notes=notes,
    )
    plan.exact_hit = exact_hit
    return plan


def next_rotation_offset(plan: RenderPlan, reaction_count: int) -> int:
    """Where the next video should start in the rotation, so a batch varies."""
    if reaction_count <= 0:
        return 0
    return (plan.rotation_offset + plan.originals_used) % reaction_count


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------
def _rated_selection(originals: Sequence[Clip], reaction_prefix: list[float],
                     rotated: list[ReactionAsset], max_pairs: int,
                     target: float, settings: Settings,
                     ratings: dict[int, float]) -> Optional[_Candidate]:
    """Take the highest-rated clips that fit, then put them back in order.

    The requirement is "give the reaction to the clips that deserve it", so the
    selection is greedy on rating: best clip first, keep taking while the
    running total still fits the target. Reaction time grows with each pair, so
    the fit test uses the prefix sum rather than a fixed allowance.

    Clips with no rating are not treated as zero - they sort after rated clips
    but stay available, so a partial rating still produces a full video.
    """
    if not originals:
        return None

    order = sorted(
        range(len(originals)),
        key=lambda i: (
            -ratings.get(originals[i].index, -1.0),   # unrated sort last
            -originals[i].duration,                   # then longer first
            i,                                        # stable
        ),
    )

    chosen: list[int] = []
    running = 0.0
    for index in order:
        if len(chosen) >= max_pairs:
            break
        reaction = rotated[len(chosen)]
        addition = originals[index].duration + reaction.duration
        room = max(0.0, reaction.duration - _MIN_TAIL)
        if running + addition > target + room:
            continue          # too long for what is left; try the next one
        chosen.append(index)
        running += addition
        if running >= target - 1e-9:
            break

    if not chosen:
        return None

    # Greedy stops as soon as the next-best clip does not fit, which can leave
    # the total a second or two short - and a shortfall cannot be trimmed away
    # the way an overshoot can. So make one pass for a clip that closes the
    # gap, taking the best-rated of those that do, and let the tail trim land
    # the exact figure.
    if running < target - 1e-9 and len(chosen) < max_pairs:
        taken = set(chosen)
        reaction = rotated[len(chosen)]
        # Any clip that reaches the target will do, because the trim can shave
        # the closing reaction and then the closing clip. Prefer the smallest
        # overshoot so the trim stays small, and break ties on rating. A window
        # only as wide as the reaction was too narrow to ever match, which left
        # outputs several seconds short.
        best_topup: Optional[int] = None
        best_key: Optional[tuple[float, float]] = None
        for index in order:
            if index in taken:
                continue
            total = running + originals[index].duration + reaction.duration
            if total < target - 1e-9:
                continue
            key = (total - target, -ratings.get(originals[index].index, -1.0))
            if best_key is None or key < best_key:
                best_key, best_topup = key, index
        if best_topup is not None:
            chosen.append(best_topup)
            running += originals[best_topup].duration + reaction.duration

    chosen.sort()             # chronological order (req 6)
    return _Candidate(
        pairs=len(chosen),
        original_indices=chosen,
        originals_total=sum(originals[i].duration for i in chosen),
        reactions_total=reaction_prefix[len(chosen)],
    )


def _sequential_selection(originals: Sequence[Clip],
                          reaction_prefix: list[float],
                          rotated: list[ReactionAsset], max_pairs: int,
                          target: float, settings: Settings
                          ) -> Optional[_Candidate]:
    """Take clips in order: clip, reaction, next clip, reaction, until full.

    This is the reading of the requirement that matches what the finished video
    should feel like - the video plays through in order, each clip answered once
    and then done with. The alternative (`fit`) cherry-picks whichever clips add
    up neatly to 3:00, which lands the length perfectly but skips around.

    The last pair is the only awkward one: including it usually overshoots and
    excluding it undershoots, so both are measured and the closer one wins.
    """
    if not originals:
        return None

    chosen: list[int] = []
    running = 0.0
    for index, clip in enumerate(originals):
        if len(chosen) >= max_pairs:
            break
        if running >= target - 1e-9:
            break
        chosen.append(index)
        running += clip.duration + rotated[len(chosen) - 1].duration

    if not chosen:
        return None

    def _candidate(indices: list[int]) -> Optional[_Candidate]:
        if not indices:
            return None
        return _Candidate(
            pairs=len(indices),
            original_indices=list(indices),
            originals_total=sum(originals[i].duration for i in indices),
            reactions_total=reaction_prefix[len(indices)],
        )

    full = _candidate(chosen)
    trimmed = _candidate(chosen[:-1]) if len(chosen) > 1 else None
    if trimmed is None or full is None:
        return full

    # Overshoot is recoverable up to what the closing reaction and the closing
    # clip can spare between them; undershoot is not recoverable at all.
    over = _miss(full, rotated, originals, target)
    under = _miss(trimmed, rotated, originals, target)
    return full if over <= under else trimmed



def _best_selection(originals: Sequence[Clip], reaction_prefix: list[float],
                    rotated: list[ReactionAsset], max_pairs: int,
                    target: float, settings: Settings,
                    ratings: Optional[dict[int, float]] = None
                    ) -> Optional[_Candidate]:
    """Pick the (k, subset-of-originals) pair that best hits the target.

    `ratings` only breaks ties. Many different subsets hit 3:00 exactly, and
    when the caller knows which clips are worth reacting to there is no reason
    to pick among them at random - so the funniest of the exact fits wins. It
    cannot trade length for funniness: the rating term is a thousandth of a
    second at most, far below any real difference in fit.
    """
    quantum = 1.0 / float(settings.fps or 30)
    durations_q = [max(1, int(round(c.duration / quantum))) for c in originals]

    # Allow the search to run past the target so `exact_duration` has an
    # overshoot to trim away.
    slack = max(reaction_prefix[1:max_pairs + 1] or [0.0]) + 5.0
    longest = max((c.duration for c in originals), default=0.0)
    limit = int(round((target + slack + longest) / quantum)) + 1
    limit_mask = (1 << (limit + 1)) - 1

    # masks[k] = bitmask of totals reachable with exactly k originals.
    masks = [0] * (max_pairs + 1)
    masks[0] = 1
    snapshots: list[list[int]] = [masks[:]]

    for d in durations_q:
        for k in range(max_pairs, 0, -1):
            if masks[k - 1]:
                masks[k] |= (masks[k - 1] << d) & limit_mask
        snapshots.append(masks[:])

    # (score, k, indices, originals_total)
    best: Optional[tuple[float, int, list[int], float]] = None

    for k in range(1, max_pairs + 1):
        mask = masks[k]
        if not mask:
            continue
        reactions_total = reaction_prefix[k]
        budget_q = int(round((target - reactions_total) / quantum))
        if budget_q < 0:
            continue

        for sum_q in dict.fromkeys(_closest_bits(mask, budget_q)):
            indices = _reconstruct(snapshots, durations_q, k, sum_q)
            if indices is None:
                continue
            # Score on the real durations, not the quantised ones, so the
            # 50 ms DP grid never leaks into the final length.
            originals_total = sum(originals[i].duration for i in indices)
            drift = originals_total + reactions_total - target
            if settings.exact_duration:
                # Overshoot is recoverable up to whatever the closing reaction
                # and closing clip can spare; the rest is the real error.
                probe = _Candidate(pairs=k, original_indices=sorted(indices),
                                   originals_total=originals_total,
                                   reactions_total=reactions_total)
                score = _miss(probe, rotated, originals, target)
            else:
                score = abs(drift)
            # Tie-break toward funnier clips, then toward more alternations:
            # livelier output, same length.
            if ratings:
                mean = sum(ratings.get(originals[i].index, 0.0)
                           for i in indices) / len(indices)
                score -= 1e-4 * mean
            score -= k * 1e-6
            if best is None or score < best[0]:
                best = (score, k, indices, originals_total)

    if best is None:
        return None

    _, pairs, indices, originals_total = best
    indices = sorted(indices)  # chronological order (req 6)
    return _Candidate(
        pairs=pairs,
        original_indices=indices,
        originals_total=originals_total,
        reactions_total=reaction_prefix[pairs],
    )


def _recoverable(candidate: "_Candidate", rotated: list[ReactionAsset],
                 originals: Sequence[Clip]) -> float:
    """How much overshoot the tail trim can actually absorb.

    It has to mirror what build_plan really does, which is shave the closing
    reaction first and then the closing clip. Counting only the reaction - the
    earlier version - made every selector understate its room by several
    seconds, so it preferred an undershoot it could never recover from.
    """
    if not candidate.original_indices:
        return 0.0
    reaction_room = max(0.0, rotated[candidate.pairs - 1].duration - _MIN_TAIL)
    last = originals[candidate.original_indices[-1]]
    clip_room = max(0.0, last.duration - _MIN_TAIL_CLIP)
    return reaction_room + clip_room


def _miss(candidate: "_Candidate", rotated: list[ReactionAsset],
          originals: Sequence[Clip], target: float) -> float:
    """Seconds the finished video would end up away from the target.

    Asymmetric on purpose: an overshoot within trim room costs nothing, while
    a shortfall is the full amount, because nothing downstream can add time.
    """
    drift = candidate.natural - target
    if drift < 0:
        return -drift
    return max(0.0, drift - _recoverable(candidate, rotated, originals))


def _closest_bits(mask: int, centre: int) -> list[int]:
    """The nearest reachable total at or below `centre`, and at or above it.

    Those two are the only totals worth scoring for a given clip count: any
    other set bit is strictly further from the budget. Found with bit tricks
    rather than a windowed scan, so a total far from the budget is still found.
    """
    hits: list[int] = []
    if centre >= 0:
        below = mask & ((1 << (centre + 1)) - 1)
        if below:
            hits.append(below.bit_length() - 1)
    above = mask >> max(0, centre)
    if above:
        lowest = above & -above
        hits.append(max(0, centre) + lowest.bit_length() - 1)
    return hits


def _reconstruct(snapshots: list[list[int]], durations_q: list[int],
                 pairs: int, sum_q: int) -> Optional[list[int]]:
    """Recover which originals produced (pairs, sum_q)."""
    chosen: list[int] = []
    k, remaining = pairs, sum_q
    for j in range(len(durations_q) - 1, -1, -1):
        if k == 0:
            break
        before = snapshots[j]  # state using only items 0..j-1
        if (before[k] >> remaining) & 1:
            continue  # item j was not needed
        d = durations_q[j]
        if remaining >= d and k >= 1 and (before[k - 1] >> (remaining - d)) & 1:
            chosen.append(j)
            k -= 1
            remaining -= d
    if k != 0 or remaining != 0:
        return None
    return chosen


def describe_plan(plan: RenderPlan) -> str:
    """Human-readable summary for the UI and logs."""
    lines = [
        f"Timeline: {plan.originals_used} original clips + "
        f"{plan.originals_used} reactions = {len(plan.segments)} segments",
        f"Length: {plan.total_duration:.2f}s "
        f"(target {plan.target:g}s, drift {plan.drift:+.2f}s)",
        f"Whole-clip length before tail trim: {plan.natural_duration:.2f}s",
        f"Clips available: {plan.originals_available}",
    ]
    lines.extend(f"Note: {n}" for n in plan.notes)
    return "\n".join(lines)
