"""
Lanes vs decks.

The timeline's lanes are NOT decks (the user). You put tracks on lanes; the solver then
picks which physical deck each lane is played on (solver.solve_best_decks). A deck can
play several lanes one after the other, as long as each track has finished in time to
load the next one (LOAD_GAP_BARS). So with 4 decks you can use 5, 6, ... lanes: a 5th
song simply goes onto a deck whose track has finished.

This module answers two questions, used by both the solver and the plan checks:
  * which lanes can share a deck (they never play, or load, at the same time)?
  * how many decks does the plan need at its busiest moment?
"""

from __future__ import annotations

from .models import Plan
from .solver_config import LOAD_GAP_BARS


def lane_intervals(plan: Plan) -> dict[int, list[tuple[float, float]]]:
    """
    For each lane: the stretches (set bars, start..end) when it has a track on its deck.
    Back-to-back blocks of the SAME track are one stretch, including any gap between
    them (the track stays loaded, maybe playing muted, to come back later). A different
    track on the same lane starts a new stretch.
    """
    out: dict[int, list[tuple[float, float]]] = {}
    for lane in sorted({s.deck for s in plan.segments}):
        stretches: list[list] = []  # [track_id, start, end]
        for s in plan.deck_segments(lane):
            if stretches and stretches[-1][0] == s.track_id:
                stretches[-1][2] = max(stretches[-1][2], s.set_end_bar)
            else:
                stretches.append([s.track_id, s.set_bar, s.set_end_bar])
        out[lane] = [(a, b) for _, a, b in stretches]
    return out


def _clash(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Do two stretches need the deck at once (counting the time to load the next one)?"""
    return a[0] < b[1] + LOAD_GAP_BARS and b[0] < a[1] + LOAD_GAP_BARS


def lanes_clash(x: list[tuple[float, float]], y: list[tuple[float, float]]) -> bool:
    """Can't two lanes share a deck? True if any of their stretches clash."""
    return any(_clash(a, b) for a in x for b in y)


def clashing_pairs(plan: Plan) -> set[frozenset]:
    """Every pair of lanes that can't share a deck, as {frozenset({lane_a, lane_b})}."""
    iv = lane_intervals(plan)
    lanes = sorted(iv)
    return {frozenset((a, b)) for i, a in enumerate(lanes) for b in lanes[i + 1:]
            if lanes_clash(iv[a], iv[b])}


def busiest_moment(plan: Plan) -> tuple[int, float, list[int]]:
    """
    (how many decks the plan needs at its busiest moment, the set bar where that is,
    the lanes busy then). A lane counts as busy from a stretch's start until
    LOAD_GAP_BARS after it ends (the deck needs that long to take the next track).
    """
    iv = lane_intervals(plan)
    best: tuple[int, float, list[int]] = (0, 1.0, [])
    # the count only goes up where a stretch starts, so checking those moments is enough
    for t in sorted({a for stretches in iv.values() for a, _ in stretches}):
        busy = [lane for lane, stretches in iv.items()
                if any(a <= t < b + LOAD_GAP_BARS for a, b in stretches)]
        if len(busy) > best[0]:
            best = (len(busy), t, busy)
    return best
