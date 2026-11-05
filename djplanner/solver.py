"""
The solver: works out HOW to physically perform a plan on the decks.

Phase 3 scope: everything that happens WITHIN one track on one deck (continuing,
hot cue jumps, beat jumps, loops), plus the basics around it (load, start, stop).
Mixing between decks (fades, BPM/key checks, choosing transitions) is Phase 4.

How it works
------------
1. The plan is split into "runs": back-to-back blocks of the SAME track on the SAME
   deck with no gap. A run is played without stopping the deck.

       Deck 1: [Intro][Chorus 2][Chorus 2]   [Outro]     <- one run, then...
                                              ^ gap: a new run (needs a new start)

2. Between neighbouring blocks in a run there's a "transition". Each transition can be
   done in several ways (an "option"), each with a cost (see solver_config.py):
       continue   - the next block follows on naturally: nothing to do
       beat jump  - jump by a power-of-two number of beats (1-2 presses)
       hot cue    - hit a pad (yours if one is already there, else a new one)
       loop       - for blocks that repeat the end of the previous one
       render     - last resort: can't be done live
   Each run also needs a "start". Preferred (Phase 4): a PRE-ROLL - press PLAY from a
   cue a few bars early with the fader DOWN, then just "fader up" on the bar (the
   user: standard practice). If the same track comes back on the same deck after a
   gap, it can instead keep running MUTED and be beat-jumped into place any time in
   the gap (a relaxed action), then fader up. Pressing PLAY / a pad right on the bar
   is the fallback.

3. Choosing the options is a SHORTEST-PATH problem: the cheapest route through all
   the choices. It's solved per TRACK (not per transition) because new hot cues share
   the track's 8 pads: setting one cue and using it 3 times can beat 3 beat jumps.
   We use Dijkstra's algorithm over states (position in the track's runs, which new
   cues exist so far, when the last action happened).

4. Afterwards: actions from all decks are merged in time order, pads are assigned to
   new cues (never one of your existing pads), "any time between" actions (relaxed
   beat jumps, deck switches) are placed where a hand is free and nearby, and your TWO
   HANDS are walked over the controller (hands.py, controllers.py): anything a hand
   can't reach in time is flagged, and pad-mode changes are written into the steps.

5. If some decks share a jog wheel (e.g. 4 decks on a 2-jog controller), a "deck
   switch" step is added wherever a side has to change deck before an action, and
   switches squeezed in too tightly are flagged (see _add_deck_switches).

Everything here is pure Python with no web or Rekordbox code, so it can be tested
with fake tracks (tests/test_solver.py).
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import hands
from . import solver_config as cfg
from .controllers import Layout
from .lanes import clashing_pairs
from .models import Action, Plan, PlanSettings, PlannedCue, Segment, Track

# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------

EPS = 1e-6


def _same(a: float, b: float, tol: float = EPS) -> bool:
    return abs(a - b) <= tol


def _fmt(bar: float) -> str:
    """33.0 -> "33", 32.75 -> "32.75"."""
    return f"{bar:g}"


def _on_beat(set_bar: float) -> str:
    """'on the 1' for a whole bar, else 'on beat 3' (which beat of the bar it falls on)."""
    beat = int(round((set_bar - int(set_bar + EPS)) * 4)) + 1
    return "on the 1" if beat == 1 else f"on beat {beat}"


def _power_of_two_beats(bars: float, sizes: list[int]) -> Optional[int]:
    """Number of beats if `bars` is exactly one of the allowed sizes, else None."""
    beats = bars * 4
    for size in sizes:
        if _same(beats, size):
            return size
    return None


def beat_jump_presses(delta_bars: float) -> Optional[list[int]]:
    """
    How to cover a jump of `delta_bars` (negative = backwards) with beat-jump presses.
    Returns the list of signed beat sizes to press (fewest presses first), or None if
    it needs more than MAX_BEAT_JUMP_PRESSES presses.
    e.g. +16 bars -> [64]; +12 bars -> [32, 16]; +3 bars -> [8, 4]; +0.75 -> [2, 1]
    """
    beats = delta_bars * 4
    if _same(beats, 0) or not _same(beats, round(beats)):
        return None
    sign = 1 if beats > 0 else -1
    target = abs(round(beats))
    sizes = sorted(cfg.BEAT_JUMP_SIZES_BEATS, reverse=True)
    for n in range(1, cfg.MAX_BEAT_JUMP_PRESSES + 1):
        for combo in itertools.combinations_with_replacement(sizes, n):
            if sum(combo) == target:
                return [sign * c for c in combo]
    return None


# --------------------------------------------------------------------------------------
# Runs: back-to-back blocks of one track on one deck
# --------------------------------------------------------------------------------------


@dataclass
class Run:
    deck: int
    track_id: str
    blocks: list[Segment]
    # The runs just before / after this one on the SAME deck (None if none). Used for
    # pre-rolls (is the deck free?) and to bring a track back after a gap.
    prev: Optional["Run"] = field(default=None, repr=False, compare=False)
    next: Optional["Run"] = field(default=None, repr=False, compare=False)

    @property
    def set_start(self) -> float:
        return self.blocks[0].set_bar

    @property
    def set_end(self) -> float:
        return self.blocks[-1].set_end_bar


def split_into_runs(plan: Plan) -> list[Run]:
    """Group each deck's blocks into runs (same track, no gap), sorted by start time."""
    runs: list[Run] = []
    for deck in sorted({s.deck for s in plan.segments}):
        current: Optional[Run] = None
        for seg in plan.deck_segments(deck):
            if (
                current is not None
                and seg.track_id == current.track_id
                and _same(seg.set_bar, current.set_end)
            ):
                current.blocks.append(seg)
            else:
                new = Run(deck, seg.track_id, [seg])
                if current is not None:
                    new.prev, current.next = current, new
                current = new
                runs.append(current)
    return sorted(runs, key=lambda r: (r.set_start, r.deck))


# --------------------------------------------------------------------------------------
# Options: the possible ways of doing one step
# --------------------------------------------------------------------------------------


@dataclass
class _Option:
    """One way of doing a step. Actions are filled in fully after the search."""

    cost: float
    actions: list[Action]
    # New cues this option needs: (track_bar, type, name). Pads are assigned later.
    new_cues: list[tuple[float, str, str]] = field(default_factory=list)
    advance_to: int = 0  # index of the block we're at after this option


class _TrackContext:
    """What the solver needs to know about one track."""

    def __init__(self, track_id: str, track: Optional[Track]):
        self.track_id = track_id
        self.track = track
        self.title = track.summary.title if track else track_id
        cues = track.cues if track else []
        # Your existing cues, which we may reuse but never change.
        self.existing_hot = {q.pad: q.bar for q in cues if q.type == "hot" and q.pad}
        # Hot cues on pads we can't identify still block a pad (we don't know which).
        self.unknown_hot = sum(1 for q in cues if q.type == "hot" and not q.pad)
        self.existing_memory = [q.bar for q in cues if q.type == "memory"]
        self.phrase_starts = [p.start_bar for p in track.phrases] if track else []
        # First bar position in the track (a pre-roll can't start before it)
        self.first_bar = track.beatgrid.beat_index_to_bar(0) if track and len(track.beatgrid) else 1.0
        self.bar_count = track.beatgrid.bar_count if track and len(track.beatgrid) else None

    @property
    def free_pads(self) -> list[str]:
        """Pads with none of your cues on them, in order."""
        free = [p for p in cfg.HOT_CUE_PADS if p not in self.existing_hot]
        return free[: max(0, len(free) - self.unknown_hot)]

    def existing_hot_at(self, bar: float) -> Optional[str]:
        for pad, cue_bar in sorted(self.existing_hot.items()):
            if _same(cue_bar, bar, cfg.CUE_MATCH_TOLERANCE_BARS):
                return pad
        return None

    def existing_memory_at(self, bar: float) -> bool:
        return any(_same(b, bar, cfg.CUE_MATCH_TOLERANCE_BARS) for b in self.existing_memory)

    def phrase_aligned(self, bar: float) -> bool:
        # Tracks with no analysis can't be judged; don't penalise them.
        return not self.phrase_starts or any(_same(bar, p, 0.01) for p in self.phrase_starts)


def _start_options(ctx: _TrackContext, run: Run, cues_so_far: frozenset,
                   settings: PlanSettings) -> list[_Option]:
    """
    Ways to start a run:
      * MUTED RE-ENTRY: the same track was on this deck before a gap. Keep it running
        with the fader down; beat jump it into place any time in the gap (if needed);
        fader up on the bar.
      * PRE-ROLL: press PLAY (or a pad) a few bars early from a cue that many bars before
        the block, with the fader down; fader up on the bar.
      * DIRECT: press PLAY from a memory cue, or hit a pad, right on the bar (fallback).
    """
    first = run.blocks[0]
    bar = first.start_bar
    S = first.set_bar
    d = run.deck
    aligned = 0 if ctx.phrase_aligned(bar) else cfg.COSTS["not_phrase_aligned"]
    base = cfg.COSTS["action"] + aligned
    name = f"Start: {first.label}"
    opts = []

    def act(text, pad=None, kind="start", at=S, track_bar=bar, params=None):
        return Action(kind=kind, deck=d, track_id=run.track_id, set_bar=at, text=text,
                      track_bar=track_bar, pad=pad, segment_id=first.id,
                      params={"label": first.label, "comes_in": S, **(params or {})})

    def cue_step(at, cue_bar):
        """Separate "go to the memory cue" step before a PLAY press (the user: not all in
        one instruction). Can be done any time before; kind "cue". If you don't have a
        memory cue there yet, it says how to make one on the fly (new_cue): it's for one
        transition at a time, with no prep list (the user)."""
        has = ctx.existing_memory_at(cue_bar) or (cue_bar, "memory") in cues_so_far
        text = (f"Deck {d}: go to your memory cue at track bar {_fmt(cue_bar)} (press CUE)" if has
                else f"Deck {d}: find track bar {_fmt(cue_bar)} and press CUE there")
        return act(text, kind="cue", at=at, track_bar=cue_bar, params={"new_cue": not has})

    fader_up = act(f"Deck {d}: fader UP {_on_beat(S)} ('{first.label}' comes in)", kind="fader_up")
    prev = run.prev
    deck_free_from = prev.set_end if prev else None

    # --- muted re-entry: same track, same deck, after a gap ---
    if prev is not None and prev.track_id == run.track_id:
        gap = S - prev.set_end
        pos_at_S = prev.blocks[-1].end_bar + gap  # where it gets to if left running
        delta = bar - pos_at_S
        fits = ctx.bar_count is None or pos_at_S <= ctx.bar_count + 1
        if fits and abs(delta) < EPS:
            opts.append(_Option(cfg.COSTS["fader"] + aligned, [
                act(f"Deck {d}: fader UP {_on_beat(S)}. It's been running muted since set bar "
                    f"{_fmt(prev.set_end)}, so it's already at track bar {_fmt(bar)} ('{first.label}')",
                    kind="fader_up", params={"reentry": True})]))
        presses = beat_jump_presses(delta) if fits else None
        if presses and gap >= 1:
            w0, w1 = prev.set_end, S - 1  # leave a bar before the fader comes up
            text = " then ".join(f"{'+' if p > 0 else '−'}{abs(p)}" for p in presses)
            bars = abs(sum(presses)) / 4
            jump = act(
                f"Deck {d} (muted): beat jump {text} beats ({_fmt(bars)} bars "
                f"{'forward' if delta > 0 else 'back'}) any time between set bar {_fmt(w0)} and "
                f"{_fmt(w1)}, so it reaches track bar {_fmt(bar)} ('{first.label}') at set bar {_fmt(S)}",
                kind="beat_jump", at=w0, track_bar=bar,
                params={"beats": presses, "presses": len(presses), "window": [w0, w1], "reentry": True})
            opts.append(_Option(cfg.COSTS["relaxed_action"] * len(presses) + cfg.COSTS["fader"] + aligned,
                                [jump, fader_up]))

    # --- pre-roll from a cue a few bars earlier ---
    pre_lengths = [n for n in cfg.PREROLL_FALLBACK_BARS if n <= settings.preroll_bars]
    for n in pre_lengths:
        at = S - n
        cue_bar = bar - n
        if at < 1 - EPS or cue_bar < ctx.first_bar - EPS:
            continue  # before the set / the track starts: no room
        if deck_free_from is not None and at < deck_free_from - EPS:
            continue  # the deck is still playing something else
        pre = f"{_fmt(n)} bar{'s' if n != 1 else ''} early, fader DOWN"
        cue_name = f"Pre-roll: {first.label}"
        pad = ctx.existing_hot_at(cue_bar)
        if pad:
            opts.append(_Option(base + cfg.COSTS["fader"], [
                act(f"Deck {d}: hit pad {pad} {_on_beat(at)} ({pre}; track bar {_fmt(cue_bar)})", pad=pad, at=at,
                    track_bar=cue_bar, params={"preroll": n}), fader_up]))
        has_mem = ctx.existing_memory_at(cue_bar) or (cue_bar, "memory") in cues_so_far
        opts.append(_Option(
            base + cfg.COSTS["fader"] + (0 if has_mem else cfg.COSTS["new_memory_cue"]),
            [cue_step(at, cue_bar),
             act(f"Deck {d}: press PLAY {_on_beat(at)} ({pre}; cued to the memory cue at track bar "
                 f"{_fmt(cue_bar)})", at=at, track_bar=cue_bar, params={"preroll": n}), fader_up],
            new_cues=[] if has_mem else [(cue_bar, "memory", cue_name)],
        ))
        break  # the longest pre-roll that fits is enough

    # --- direct start, right on the bar (fallback; normal for the set's first track) ---
    exact = 0 if S <= 1 + EPS else cfg.COSTS["exact_start"]
    pad = ctx.existing_hot_at(bar)
    if pad:
        opts.append(_Option(base + exact, [act(f"Hit pad {pad} to start '{first.label}'", pad=pad)]))
    has_mem = ctx.existing_memory_at(bar) or (bar, "memory") in cues_so_far
    opts.append(_Option(
        base + exact + (0 if has_mem else cfg.COSTS["new_memory_cue"]),
        [cue_step(S, bar), act(f"Press PLAY (cued to the memory cue at track bar {_fmt(bar)}, '{first.label}')")],
        new_cues=[] if has_mem else [(bar, "memory", name)],
    ))
    if not pad:
        has_hot = (bar, "hot") in cues_so_far
        opts.append(_Option(
            base + exact + (0 if has_hot else cfg.COSTS["new_hot_cue"]),
            [act(f"Hit pad {{pad}} to start '{first.label}'", params={"new_cue": not has_hot})],
            new_cues=[] if has_hot else [(bar, "hot", name)],
        ))
    return opts


def _transition_options(ctx: _TrackContext, run: Run, i: int, cues_so_far: frozenset) -> list[_Option]:
    """
    Ways to get from block i to block i+1 (and, for loops, past several identical
    blocks at once). The deck has just played block i up to its end.
    """
    A = run.blocks[i]
    B = run.blocks[i + 1]
    boundary = B.set_bar  # set bar where B must start
    a0, a1 = A.start_bar, A.end_bar
    b0 = B.start_bar
    opts: list[_Option] = []

    def act(kind, set_bar, text, params=None, **kw):
        return Action(kind=kind, deck=run.deck, track_id=run.track_id, set_bar=set_bar,
                      text=text, segment_id=B.id, params={"label": B.label, **(params or {})}, **kw)

    # --- 1. continue: B carries straight on from A ---
    if _same(b0, a1):
        return [_Option(0.0, [], advance_to=i + 1)]

    aligned_penalty = 0 if ctx.phrase_aligned(b0) else cfg.COSTS["not_phrase_aligned"]

    # --- 2. loop: B (and any identical blocks after it) repeat the END of A ---
    # e.g. A = track bars 17-32, B = 25-32 -> loop bars 25-32 (4 bars... here 8)
    if _same(B.end_bar, a1) and a0 - EPS <= b0 < a1:
        L = B.length_bars
        j = i + 1  # last block covered by the loop
        while (j + 1 < len(run.blocks)
               and _same(run.blocks[j + 1].start_bar, B.start_bar)
               and _same(run.blocks[j + 1].length_bars, L)):
            j += 1
        for last in range(i + 1, j + 1):  # loop may cover some or all of the repeats
            n_passes = 1 + (last - i)
            engage_at = boundary - L  # when the deck reaches b0 during A
            end_at = run.blocks[last].set_end_bar
            exit_at = end_at - L  # start of the last pass
            auto = _power_of_two_beats(L, cfg.AUTO_LOOP_SIZES_BEATS)
            loop_desc = f"track bars {_fmt(b0)}–{_fmt(a1 - 1)}"
            params = {"loop_bars": L, "passes": n_passes}
            if auto:
                actions = [act("loop_in", engage_at,
                               f"Press {auto}-beat auto loop ({_fmt(L)} bars) on the 1, looping {loop_desc}, "
                               f"{n_passes} passes in total",
                               track_bar=b0, params={**params, "method": "auto", "beats": auto})]
                cost = cfg.COSTS["action"]
            else:
                actions = [
                    act("loop_in", engage_at, f"LOOP IN on the 1 (track bar {_fmt(b0)})",
                        track_bar=b0, params={**params, "method": "manual"}),
                    act("loop_out", boundary - 0.25,
                        f"LOOP OUT on beat 4, so it loops {loop_desc} ({_fmt(L)} bars, "
                        f"{n_passes} passes in total)",
                        track_bar=a1, params={**params, "method": "manual"}),
                ]
                cost = cfg.COSTS["action"] * 2 + cfg.COSTS["manual_loop"]
            actions.append(act("loop_exit", exit_at,
                               f"LOOP EXIT during the last pass (any time before set bar {_fmt(end_at)})",
                               track_bar=b0, params=params))
            cost += cfg.COSTS["action"]
            opts.append(_Option(cost + aligned_penalty, actions, advance_to=last))

    # --- 3. beat jump by a power-of-two number of beats ---
    presses = beat_jump_presses(b0 - a1)
    if presses:
        text = " then ".join(f"{'+' if p > 0 else '−'}{abs(p)}" for p in presses)
        cost = (cfg.COSTS["action"] + cfg.COSTS["beat_jump_timing"]) * len(presses)
        cost += cfg.COSTS["extra_beat_jump_press"] * (len(presses) - 1)
        opts.append(_Option(
            cost + aligned_penalty,
            [act("beat_jump", boundary,
                 f"Beat jump {text} beats right on the 1 (→ track bar {_fmt(b0)}, '{B.label}')",
                 track_bar=b0, params={"beats": presses, "presses": len(presses)})],
            advance_to=i + 1,
        ))

    # --- 4. hot cue jump: quantize makes the jump land on the next beat, so press it
    #        during the last beat before the boundary ---
    press_at = boundary - 0.25
    name = f"→ {B.label}"
    pad = ctx.existing_hot_at(b0)
    if pad:
        opts.append(_Option(
            cfg.COSTS["action"] + aligned_penalty,
            [act("hot_cue", press_at, f"Hit pad {pad} on beat 4 (→ track bar {_fmt(b0)}, '{B.label}')",
                 track_bar=b0, pad=pad)],
            advance_to=i + 1,
        ))
    else:
        has_hot = (b0, "hot") in cues_so_far
        opts.append(_Option(
            cfg.COSTS["action"] + aligned_penalty + (0 if has_hot else cfg.COSTS["new_hot_cue"]),
            [act("hot_cue", press_at, f"Hit pad {{pad}} on beat 4 (→ track bar {_fmt(b0)}, '{B.label}')",
                 track_bar=b0, params={"new_cue": not has_hot})],
            new_cues=[] if has_hot else [(b0, "hot", name)],
            advance_to=i + 1,
        ))

    # --- 5. last resort: render as an audio edit ---
    opts.append(_Option(
        cfg.COSTS["render_fallback"],
        [act("render", boundary,
             f"Can't be played live (no free pads and no beat jump fits). Render the change into "
             f"'{B.label}' as an audio edit.", track_bar=b0)],
        advance_to=i + 1,
    ))
    return opts


# --------------------------------------------------------------------------------------
# The search (Dijkstra) for one track
# --------------------------------------------------------------------------------------


def _solve_track(ctx: _TrackContext, runs: list[Run],
                 settings: Optional[PlanSettings] = None) -> tuple[float, list[Action], list[tuple]]:
    """
    Cheapest way to play all `runs` of one track.
    Returns (total cost, actions, new cues [(track_bar, type, name)] in creation order).

    State = (run index, block index within run, new cues so far, last action time,
             deck of last action). The search moves from state to state by taking one
    option; Dijkstra's algorithm finds the cheapest sequence.
    """
    settings = settings or PlanSettings()
    free_pads = len(ctx.free_pads)
    counter = itertools.count()  # tie-breaker so the heap never compares states

    # Each heap item: (cost, tiebreak, run_i, block_i, cues(frozenset), cue_order(tuple),
    #                  last_action_set_bar, actions(tuple))
    # block_i = -1 means "run not started yet".
    heap = [(0.0, next(counter), 0, -1, frozenset(), (), None, ())]
    best: dict = {}

    while heap:
        cost, _, ri, bi, cues, order, last_t, actions = heapq.heappop(heap)
        if ri == len(runs):
            return cost, list(actions), list(order)
        key = (ri, bi, cues, last_t)
        if key in best and best[key] <= cost:
            continue
        best[key] = cost

        run = runs[ri]
        if bi == -1:
            options = _start_options(ctx, run, cues, settings)
        else:
            options = _transition_options(ctx, run, bi, cues)

        for opt in options:
            new_cues = cues
            new_order = order
            for (bar, typ, name) in opt.new_cues:
                if (bar, typ) not in new_cues:
                    new_cues = new_cues | {(bar, typ)}
                    new_order = new_order + ((bar, typ, name),)
            # Out of pads? This option isn't possible.
            if sum(1 for (_, t) in new_cues if t == "hot") > free_pads:
                continue

            # Tight-timing penalty: an action too soon after the previous one on this deck
            extra = 0.0
            t = last_t
            for a in opt.actions:
                if "window" in a.params or a.kind == "cue":
                    continue  # any time in its window / any time before: no timing pressure
                if t is not None and a.set_bar - t < cfg.AWKWARD_WINDOW_BARS - EPS:
                    extra += cfg.COSTS["tight_timing"]
                t = a.set_bar
            # A new run (possibly on another deck) resets the timing chain
            nxt_bi = 0 if bi == -1 else opt.advance_to
            nxt_ri = ri
            if nxt_bi == len(run.blocks) - 1:
                nxt_ri, nxt_bi = ri + 1, -1
            last_for_next = t if nxt_ri == ri else None
            heapq.heappush(heap, (
                cost + opt.cost + extra, next(counter), nxt_ri, nxt_bi,
                new_cues, new_order, last_for_next, actions + tuple(opt.actions),
            ))
    raise RuntimeError("solver found no route (render fallback should always exist)")


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


@dataclass
class Solution:
    actions: list[Action]
    cues: list[PlannedCue]  # every cue the plan uses (new + your existing ones)
    cost: float
    issues: list[dict]  # [{level, message, action_index}]
    hand_travel: float = 0.0  # seconds your hands spend moving between controls
    # Which physical deck each timeline LANE is played on (solve_best_decks); the
    # actions above use the physical decks. {} = lanes are decks (lane 1 = Deck 1...).
    deck_map: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "cues": [c.to_dict() for c in self.cues],
            "cost": round(self.cost, 2),
            "issues": self.issues,
            "deck_map": {str(k): v for k, v in self.deck_map.items()},
        }


def solve(plan: Plan, get_track: Callable[[str], Track]) -> Solution:
    """
    Work out how to perform `plan`. get_track(id) returns a Track (or raises KeyError
    if it's not in the library; the solver then assumes it has no cues/phrases).
    """
    runs = split_into_runs(plan)
    actions: list[Action] = []
    cues: list[PlannedCue] = []
    total = 0.0

    for track_id in dict.fromkeys(r.track_id for r in runs):  # first-appearance order
        try:
            track = get_track(track_id)
        except KeyError:
            track = None
        ctx = _TrackContext(track_id, track)
        track_runs = [r for r in runs if r.track_id == track_id]
        cost, track_actions, new_cues = _solve_track(ctx, track_runs, plan.settings)
        total += cost

        # Give each new hot cue a free pad (in the order they're first needed) and
        # write the pad letters into the instructions.
        pads = iter(ctx.free_pads)
        pad_for: dict[float, str] = {}
        for bar, typ, name in new_cues:
            if typ == "hot":
                pad_for[bar] = next(pads)
                cues.append(PlannedCue(track_id, "hot", bar, name, pad=pad_for[bar]))
            else:
                cues.append(PlannedCue(track_id, "memory", bar, name))
        for a in track_actions:
            if a.kind in ("hot_cue", "start") and a.pad is None and a.track_bar in pad_for \
                    and "{pad}" in a.text:
                a.pad = pad_for[a.track_bar]
            a.text = a.text.replace("{pad}", a.pad or "?")
        # Existing cues the plan relies on (listed so the cheat sheet can mention them)
        used_existing = {a.pad for a in track_actions if a.pad and a.pad in ctx.existing_hot}
        for pad in sorted(used_existing):
            cues.append(PlannedCue(track_id, "hot", ctx.existing_hot[pad],
                                   f"(your cue on pad {pad})", pad=pad, existing=True))

        # Load before each run (unless it's still loaded: the same track coming back on
        # the same deck), and stop at the end of each run. If the track comes back on
        # this deck by running MUTED through the gap, the "stop" is just fader down.
        reentry_runs = {a.segment_id for a in track_actions if a.params.get("reentry")}
        for r in track_runs:
            first_action = min((a.set_bar for a in track_actions if a.segment_id == r.blocks[0].id),
                               default=r.set_start)
            if not (r.prev is not None and r.prev.track_id == track_id):
                actions.append(Action(
                    kind="load", deck=r.deck, track_id=track_id, set_bar=min(r.set_start, first_action),
                    text=f"Load '{ctx.title}' on Deck {r.deck}", segment_id=r.blocks[0].id,
                    params={"title": ctx.title},
                ))
            if r.next is not None and r.next.blocks[0].id in reentry_runs:
                actions.append(Action(
                    kind="fader_down", deck=r.deck, track_id=track_id, set_bar=r.set_end,
                    text=f"Deck {r.deck}: fader DOWN {_on_beat(r.set_end)}, but leave it PLAYING (muted) "
                         f"(end of '{r.blocks[-1].label}'; it comes back at set bar {_fmt(r.next.set_start)})",
                    segment_id=r.blocks[-1].id,
                    params={"label": r.blocks[-1].label, "comes_back": r.next.set_start},
                ))
            else:
                actions.append(Action(
                    kind="stop", deck=r.deck, track_id=track_id, set_bar=r.set_end,
                    text=f"Stop Deck {r.deck} / fader down (end of '{r.blocks[-1].label}')",
                    segment_id=r.blocks[-1].id, params={"label": r.blocks[-1].label},
                ))
        actions.extend(track_actions)

    # Put everything in time order. At the same moment: load first, stop before starts
    # on other decks, then everything else.
    order = {"load": 0, "cue": 0.5, "pad_mode": 0.7, "stop": 1}
    actions.sort(key=lambda a: (a.set_bar, order.get(a.kind, 2), a.deck))

    # Your two hands on the controller: place the "any time" actions where a hand is
    # free and close, then walk both hands through everything (hands.py).
    # On shared jog wheels the order of each side's actions is fixed first, so an "any
    # time" action can't drift past a moment when its side is needed on another deck;
    # the deck switches are added AFTER the placing, so each one lands between the two
    # actions it's really between (it used to be worked out from the unplaced times:
    # a beat jump on Deck 2 could end up after "Right side -> Deck 4").
    bpm = next((s.bpm for s in plan.segments if s.bpm), 0.0)
    layout = Layout(plan.settings.decks, plan.settings.jog_wheels)
    _keep_jog_order(actions, plan.settings)
    hands.place_relaxed(actions, layout, bpm)
    actions.sort(key=lambda a: (a.set_bar, order.get(a.kind, 2), a.deck))
    actions, switch_issues = _add_deck_switches(actions, plan.settings)
    hands.place_relaxed(actions, layout, bpm, kinds={"deck_switch"})
    actions.sort(key=lambda a: (a.set_bar, order.get(a.kind, 2), a.deck))
    stats: dict = {}
    actions, hand_issues = hands.simulate(actions, layout, bpm, stats=stats)
    issues = hand_issues + switch_issues
    for w in issues:  # point each issue at its action in the final list
        if "_action" in w:
            w["action_index"] = actions.index(w.pop("_action"))
    for i, a in enumerate(actions):
        if a.kind == "render":
            issues.append({"level": "error", "message": a.text, "action_index": i})
    issues += _doubles_on_one_jog(plan, actions)
    return Solution(actions=actions, cues=cues, cost=total, issues=issues,
                    hand_travel=stats.get("travel", 0.0))


# --------------------------------------------------------------------------------------
# Which lane goes on which deck
# --------------------------------------------------------------------------------------
#
# The timeline's lanes are NOT fixed to Decks 1-4 (the user): put tracks on any lane and
# the solver picks the physical deck for each lane that makes the transition easiest,
# then tells you which track to load on which deck. E.g. on a 4-deck / 2-jog controller,
# moving a lane from Deck 3 to Deck 4 can save a deck switch on the left side, or keep
# a hand from crossing the controller.

# How the deck assignments are compared (lower = easier). Tune here.
DECK_CHOICE_WEIGHTS = {
    "cost": 1.0,          # the solver's own cost (button presses, new cues...)
    "deck_switch": 2.0,   # each deck-select press on a shared jog wheel
    "travel": 0.5,        # per second of hand travel
    "too_tight": 20.0,    # each "too tight" moment
    "busy": 3.0,          # each "busy" moment
    "moved_lane": 0.5,    # per lane moved: only move lanes for a real gain (a press, a
                          # switch, a tight moment), not a few cm less hand travel
    "double_one_jog": 25.0,  # each double with both tracks on ONE jog wheel: almost a
                             # no-go (user), worth a dozen deck switches to avoid
}
# Most assignments to try in full; with more lanes than that, swap pairs until nothing
# improves.
MAX_DECK_ASSIGNMENTS = 120


def _score(sol: Solution, mapping: dict) -> float:
    w = DECK_CHOICE_WEIGHTS
    switches = sum(1 for a in sol.actions if a.kind == "deck_switch")
    tight = sum(1 for i in sol.issues if i["message"].startswith("Too tight"))
    busy = sum(1 for i in sol.issues if i["message"].startswith("Busy"))
    moved = sum(1 for lane, deck in mapping.items() if lane != deck)
    doubles = sum(1 for i in sol.issues if i["message"].startswith(DOUBLE_ONE_JOG))
    return (w["cost"] * sol.cost + w["deck_switch"] * switches + w["travel"] * sol.hand_travel
            + w["too_tight"] * tight + w["busy"] * busy + w["moved_lane"] * moved
            + w["double_one_jog"] * doubles)


# Start of the warning for two tracks playing together on one jog wheel (also how
# _score counts them).
DOUBLE_ONE_JOG = "Double on one jog wheel"


def _doubles_on_one_jog(plan: Plan, actions: list[Action]) -> list[dict]:
    """
    A warning for every DOUBLE (two decks playing at the same time, see
    mixing.find_mixes) where both decks share a jog wheel. The user never wants that,
    "just in case": while both play you may need to nudge or jump either one, and a
    shared jog would mean switching decks in the middle of the mix.
    The warning points at the incoming track's first step (its start, if found).
    """
    from .mixing import find_mixes  # (mixing imports this module too)

    st = plan.settings
    if not st.shared_jogs:
        return []
    out = []
    for m in find_mixes(plan):
        a, b = m.out_seg.deck, m.in_seg.deck
        if a > st.decks or b > st.decks or st.jog_of(a) != st.jog_of(b):
            continue
        side = st.jog_name(st.jog_of(a))
        others = [d for d in range(1, st.decks + 1) if st.jog_of(d) != st.jog_of(a)]
        hint = (f" Put one of them on {' or '.join(f'Deck {d}' for d in others)}." if others else "")
        idx = next((i for i, x in enumerate(actions)
                    if x.segment_id == m.in_seg.id and x.kind == "start"),
                   next((i for i, x in enumerate(actions) if x.segment_id == m.in_seg.id), None))
        out.append({
            "level": "warning",
            "message": f"{DOUBLE_ONE_JOG} ({side}): Deck {a} and Deck {b} play together from set bar "
                       f"{_fmt(m.start)} until {_fmt(m.end)}, so you can't touch one without switching "
                       f"decks mid-mix.{hint}",
            "action_index": idx,
        })
    return out


def remap_decks(plan: Plan, mapping: dict) -> Plan:
    """A copy of `plan` with each block moved from its lane to mapping[lane]'s deck."""
    data = plan.to_dict()
    for s in data["segments"]:
        s["deck"] = mapping.get(s["deck"], s["deck"])
    return Plan.from_dict(data)


def _deck_assignments(lanes: list[int], decks: list[int], clashes: set[frozenset],
                      limit: int) -> list[dict]:
    """
    Up to `limit` ways to put the lanes on decks with no two clashing lanes on the same
    deck. Each lane tries its own number first, so your lanes as drawn come first when
    they work.
    """
    out: list[dict] = []

    def place(i: int, mapping: dict) -> None:
        if len(out) >= limit:
            return
        if i == len(lanes):
            out.append(dict(mapping))
            return
        lane = lanes[i]
        for d in sorted(decks, key=lambda d: d != lane):
            if all(mapping[o] != d for o in mapping if frozenset((o, lane)) in clashes):
                mapping[lane] = d
                place(i + 1, mapping)
                del mapping[lane]

    place(0, {})
    return out


def solve_best_decks(plan: Plan, get_track: Callable[[str], Track]) -> tuple[Plan, Solution]:
    """
    Try putting the timeline's lanes on different physical decks and keep the easiest
    (see DECK_CHOICE_WEIGHTS). Returns (the plan with blocks on their physical decks,
    its solution with deck_map = {lane: deck}).
    """
    lanes = sorted({s.deck for s in plan.segments})
    decks = list(range(1, plan.settings.decks + 1))
    identity = {lane: lane for lane in lanes}
    # Lanes can take turns on a deck (lanes.py): only lanes that need a deck at the same
    # time (or too close to load in between) must go on different decks.
    clashes = clashing_pairs(plan)
    candidates = _deck_assignments(lanes, decks, clashes, MAX_DECK_ASSIGNMENTS + 1)
    if not lanes or not candidates:
        # nothing to place, or more lanes busy at once than there are decks (the plan
        # checks flag that): solve it with lanes as decks so there's still something
        sol = solve(plan, get_track)
        sol.deck_map = identity
        return plan, sol

    def evaluate(mapping):
        p = remap_decks(plan, mapping)
        sol = solve(p, get_track)
        return _score(sol, mapping), p, sol

    def valid(mapping):
        return all(mapping[a] != mapping[b] for a, b in map(tuple, clashes))

    best = (float("inf"), plan, None, identity)
    if len(candidates) <= MAX_DECK_ASSIGNMENTS:
        for mapping in candidates:
            score, p, sol = evaluate(mapping)
            if score < best[0] - 1e-9:
                best = (score, p, sol, mapping)
    else:
        # too many to try them all: start from the first one found (your own lanes if
        # they work) and keep making single changes while one helps: move one lane to
        # another deck, or swap everything on two decks
        best = (*evaluate(candidates[0]), candidates[0])
        improved = True
        while improved:
            improved = False
            trials = [{**best[3], lane: d} for lane in lanes for d in decks if d != best[3][lane]]
            trials += [{lane: (b if d == a else a if d == b else d) for lane, d in best[3].items()}
                       for a, b in itertools.combinations(decks, 2)]
            for trial in trials:
                if not valid(trial):
                    continue
                score, p, sol = evaluate(trial)
                if score < best[0] - 1e-9:
                    best, improved = (score, p, sol, trial), True
    _, p, sol, mapping = best
    sol.deck_map = mapping
    return p, sol


# --------------------------------------------------------------------------------------
# Shared jog wheels: switching a side between decks
# --------------------------------------------------------------------------------------

# Actions done with a deck's OWN controls (jog wheel, pads, PLAY, loop and beat-jump
# buttons). On a shared jog wheel these only work on the deck that side is switched
# to. Loading (browse knob / laptop) and stopping (channel fader) don't need a switch.
DECK_CONTROL_ACTIONS = {"start", "hot_cue", "beat_jump", "loop_in", "loop_out", "loop_exit"}


def _keep_jog_order(actions: list[Action], settings: PlanSettings) -> None:
    """
    Shared jog wheels: narrow the window of each "any time" deck-control action (e.g. a
    relaxed beat jump) so it stays between the side's neighbouring actions on OTHER
    decks, with SWITCH_WINDOW_BARS spare for the deck switch in between. E.g. a beat
    jump on Deck 2 that could be done up to bar 51, while the right side is needed on
    Deck 4 at bar 49, must be done by bar 47. `actions` in time order; in place.
    """
    if not settings.shared_jogs:
        return
    by_jog: dict[int, list[Action]] = {}
    for a in actions:
        if a.kind in DECK_CONTROL_ACTIONS:
            by_jog.setdefault(settings.jog_of(a.deck), []).append(a)
    gap = cfg.SWITCH_WINDOW_BARS
    for seq in by_jog.values():
        for i, a in enumerate(seq):
            if "window" not in a.params:
                continue
            w0, w1 = a.params["window"]
            # the nearest actions on this side for another deck (only fixed ones: two
            # "any time" actions next to each other are rare and placed in turn anyway)
            prev = next((p for p in reversed(seq[:i]) if p.deck != a.deck), None)
            nxt = next((n for n in seq[i + 1:] if n.deck != a.deck), None)
            if nxt is not None and "window" not in nxt.params:
                w1 = max(w0, min(w1, nxt.set_bar - gap))
            if prev is not None and "window" not in prev.params:
                w0 = min(w1, max(w0, prev.set_bar + gap))
            a.params["window"] = [w0, w1]
            a.set_bar = min(max(a.set_bar, w0), w1)


def _add_deck_switches(actions: list[Action], settings: PlanSettings) -> tuple[list[Action], list[dict]]:
    """
    Add a "deck_switch" step before every action that needs its side switched to
    another deck, e.g. "Left side → Deck 3". Each side starts on its lowest deck
    (Deck 1 on the left, Deck 2 on the right), as controllers do when switched on.

    The switch can be pressed any time between the side's previous action (on the
    other deck) and this one. If that window is shorter than SWITCH_WINDOW_BARS, both
    actions are flagged and a warning suggests moving one block to another side.

    `actions` must already be in time order. Returns (actions with switches added,
    warnings); the warnings' action_index points into the returned list.
    """
    if not settings.shared_jogs:
        return actions, []

    selected = {j: j for j in range(1, settings.jog_wheels + 1)}  # jog -> deck it's on
    last_use: dict[int, Action] = {}  # jog -> its most recent deck-control action
    out: list[Action] = []
    tight: list[tuple[Action, Action, Action]] = []  # (previous action, switch, action)

    for a in actions:
        if a.kind in DECK_CONTROL_ACTIONS:
            jog = settings.jog_of(a.deck)
            side = settings.jog_name(jog)
            if selected[jog] != a.deck:
                prev = last_use.get(jog)
                window = (f"any time between set bar {_fmt(prev.set_bar)} and {_fmt(a.set_bar)}" if prev
                          else f"any time before set bar {_fmt(a.set_bar)}")
                # any time after the side's last use on the other deck, up to a beat
                # before it's needed (hands.place_relaxed picks the moment)
                w0 = prev.set_bar + 0.25 if prev else max(1.0, a.set_bar - 8)
                w1 = max(w0, a.set_bar - 0.25)
                switch = Action(
                    kind="deck_switch", deck=a.deck, track_id=a.track_id, set_bar=w1,
                    text=f"{side} side → Deck {a.deck}: press its deck-select button ({window})",
                    segment_id=a.segment_id,
                    params={"jog": jog, "from_deck": selected[jog],
                            "after": prev.set_bar if prev else None, "window": [w0, w1]},
                )
                out.append(switch)
                if prev and a.set_bar - prev.set_bar < cfg.SWITCH_WINDOW_BARS - EPS:
                    tight.append((prev, switch, a))
                selected[jog] = a.deck
            last_use[jog] = a
        out.append(a)

    issues = []
    for prev, switch, a in tight:
        for x in (prev, switch, a):
            if "awkward" not in x.flags:
                x.flags.append("awkward")
        jog = settings.jog_of(a.deck)
        side = settings.jog_name(jog)
        others = [d for d in range(1, settings.decks + 1) if settings.jog_of(d) != jog]
        hint = (f" Move one of the blocks to a deck on another side ({', '.join(f'Deck {d}' for d in others)})."
                if others else "")
        issues.append({
            "level": "warning",
            "message": f"{side} side: Deck {prev.deck} needs it at set bar {_fmt(prev.set_bar)} and "
                       f"Deck {a.deck} at set bar {_fmt(a.set_bar)}, only {(a.set_bar - prev.set_bar) * 4:g} "
                       f"beat(s) apart, with a deck switch in between.{hint}",
            "action_index": out.index(a),
            "_action": a,  # (solve() re-points action_index after its final sort)
        })
    return out, issues
