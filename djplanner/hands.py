"""
Your two hands, moving over the controller through the set.

Replaces "count the actions": what makes a moment hard is how far your hands have to
MOVE (the user: a fader right after a fader is basically nothing; the top-left of the
left deck then the bottom-right of the right deck takes time; better to finish one
side before moving to the other). Positions come from controllers.py.

The maths (all in seconds):
  * travel between two controls = 0 if they're within NEAR_CM (sliding along the
    fader row, pad -> another pad), else MOVE_BASE_S + distance / HAND_SPEED_CM_S.
    E.g. left PLAY -> right pads (55 cm) = 0.25 + 55/60 = about 1.2 s.
  * using a control takes PRESS_S (a button / pad) or FADER_S (a fader move).
  * PAD MODES: the pads do hot cues in HOT CUE mode, beat jumps in BEAT JUMP mode and
    auto loops in BEAT LOOP mode (SHIFT + BEAT JUMP). Each deck remembers its mode
    (starts in HOT CUE). Needing a different mode = one extra press on the mode button
    first, added as its own step ("Pads → BEAT JUMP mode") just before.
  * The two hands start over their own side's pads. Each timed action goes to the hand
    that can get there first. If neither can get there in time: TOO TIGHT (warning,
    saying which move). If it only just makes it (less than BUSY_SLACK_S spare): a
    BUSY note.
  * "Any time between" actions (relaxed beat jumps, deck switches) are placed where a
    hand is free and already nearby (least extra travel), so one side's prep gets
    grouped together.

Pure logic, tested in tests/test_hands.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .controllers import PAD_MODES, Layout, distance
from .models import Action

NEAR_CM = 8.0  # FLX6: faders 3.8 cm apart (7.6 for two over), pads A->H 8.9 cm
HAND_SPEED_CM_S = 60.0
MOVE_BASE_S = 0.25
PRESS_S = 0.15
FADER_S = 0.2
BUSY_SLACK_S = 0.25

FADER_KINDS = {"fader_up", "fader_down", "stop"}
UNTIMED_KINDS = {"load", "render", "cue", "pad_mode"}
PAD_LETTERS = "ABCDEFGH"


def travel(a, b) -> float:
    d = distance(a, b)
    return 0.0 if d <= NEAR_CM else MOVE_BASE_S + d / HAND_SPEED_CM_S


@dataclass
class Target:
    """What an action needs a hand to do: optionally a pad-mode press, then a control."""

    pos: tuple[float, float]
    name: str  # e.g. "Deck 1 fader", "Deck 2 PLAY", "Deck 4 pads"
    use_s: float  # how long using it takes
    mode: Optional[str] = None  # pad mode this needs ("hot_cue" / "beat_jump" / "beat_loop")


def target_of(a: Action, layout: Layout) -> Optional[Target]:
    """The control an action uses (None = not a hand-timed action, e.g. loading)."""
    d = a.deck
    if a.kind in UNTIMED_KINDS:
        return None
    if a.kind in FADER_KINDS:
        return Target(layout.fader(d), f"Deck {d} fader", FADER_S)
    if a.kind == "deck_switch":
        return Target(layout.side_control(d, "deck_select"), f"{'left' if layout.side_of(d) == 1 else 'right'} DECK button", PRESS_S)
    if a.kind in ("start", "hot_cue") and a.pad and a.pad in PAD_LETTERS:
        return Target(layout.pad(d, PAD_LETTERS.index(a.pad)), f"Deck {d} pad {a.pad}", PRESS_S, "hot_cue")
    if a.kind in ("start", "hot_cue"):
        return Target(layout.side_control(d, "play"), f"Deck {d} PLAY", PRESS_S)
    if a.kind == "beat_jump":
        presses = max(1, int(a.params.get("presses", 1)))
        return Target(layout.pad(d), f"Deck {d} pads (beat jump)", PRESS_S * presses, "beat_jump")
    if a.kind == "loop_in" and a.params.get("method") == "auto":
        return Target(layout.pad(d), f"Deck {d} pads (beat loop)", PRESS_S, "beat_loop")
    if a.kind == "loop_in":
        return Target(layout.side_control(d, "loop_in"), f"Deck {d} loop IN", PRESS_S)
    if a.kind == "loop_out":
        return Target(layout.side_control(d, "loop_out"), f"Deck {d} loop OUT", PRESS_S)
    if a.kind == "loop_exit":
        return Target(layout.side_control(d, "reloop"), f"Deck {d} RELOOP/EXIT", PRESS_S)
    return None


@dataclass
class Hand:
    name: str  # "left" / "right"
    pos: tuple[float, float]
    free_at: float = float("-inf")  # seconds: when it's done with its last action
    last_name: str = "rest"


def _seconds(set_bar: float, bpm: float) -> float:
    return (set_bar - 1) * 240.0 / (bpm or 174.0)


def _route(hand: Hand, tgt: Target, need_mode: bool, layout: Layout, deck: int) -> float:
    """Seconds from the hand being free to it reaching the final control (incl. mode press)."""
    t = 0.0
    pos = hand.pos
    if need_mode:
        mb = layout.mode_button(deck, tgt.mode)
        t += travel(pos, mb) + PRESS_S
        pos = mb
    return t + travel(pos, tgt.pos)


def simulate(actions: list[Action], layout: Layout, bpm: float,
             annotate: bool = True, stats: Optional[dict] = None) -> tuple[list[Action], list[dict]]:
    """
    Walk both hands through the timed actions (in time order). Returns (actions, issues):
    the actions with a "pad_mode" step added before each one that needs the pads in
    another mode, and issues [{level, message, _action}] (_action = the action it's
    about; solve() turns that into an index). If annotate, also sets each action's
    params["hand"] and flags "awkward" (too tight) / "busy" (only just).
    `stats` (a dict, optional) gets "travel": total seconds your hands spend moving.
    """
    hands = [Hand("left", layout.home("left")), Hand("right", layout.home("right"))]
    mode = {}  # deck -> current pad mode
    issues = []
    mode_steps = []  # (action that needs it, pad-mode step to put before it)
    total_travel = 0.0
    for i, a in enumerate(actions):
        tgt = target_of(a, layout)
        if tgt is None:
            continue
        t = _seconds(a.set_bar, bpm)
        need_mode = tgt.mode is not None and mode.get(a.deck, "hot_cue") != tgt.mode
        options = []
        for h in hands:
            arrive = h.free_at + _route(h, tgt, need_mode, layout, a.deck)
            # prefer the hand on the control's own side when both are in time
            on_side = (h.name == "left") == (tgt.pos[0] < layout.fader(1)[0] + 2)
            options.append((max(arrive, float("-inf")), not on_side, h))
        in_time = [o for o in options if o[0] <= t]
        arrive, _, hand = min(in_time or options, key=lambda o: (o[1], o[0]) if in_time else (o[0], o[1]))
        slack = t - arrive
        total_travel += _route(hand, tgt, need_mode, layout, a.deck)
        if annotate:
            a.params["hand"] = hand.name
            if need_mode and "pad_mode" not in a.params:
                a.params["pad_mode"] = tgt.mode
                mode_steps.append((a, Action(
                    kind="pad_mode", deck=a.deck, track_id=a.track_id, set_bar=a.set_bar,
                    text=f"Deck {a.deck}: pads → {PAD_MODES[tgt.mode]} mode"
                         + (" (hold SHIFT + press BEAT JUMP)" if tgt.mode == "beat_loop" else ""),
                    segment_id=a.segment_id,
                    params={"mode": tgt.mode, "hand": hand.name, **({"window": a.params["window"]}
                                                                    if "window" in a.params else {})})))
        if slack < 0:
            if annotate and "awkward" not in a.flags:
                a.flags.append("awkward")
            gap = t - hand.free_at if hand.free_at > float("-inf") else 0
            issues.append({
                "level": "warning", "_action": a,
                "message": f"Too tight at set bar {a.set_bar:g}: your {hand.name} hand has to get from "
                           f"{hand.last_name} to {tgt.name}{' (changing pad mode on the way)' if need_mode else ''} "
                           f"in {max(gap, 0):.1f} s, but that takes about {gap - slack:.1f} s. "
                           "Move a block, or do something earlier so it's ready.",
            })
        elif slack < BUSY_SLACK_S and hand.free_at > float("-inf"):
            if annotate and "busy" not in a.flags:
                a.flags.append("busy")
            issues.append({
                "level": "info", "_action": a,
                "message": f"Busy moment at set bar {a.set_bar:g}: your {hand.name} hand only just gets from "
                           f"{hand.last_name} to {tgt.name} in time.",
            })
        if need_mode:
            mode[a.deck] = tgt.mode
        hand.pos = tgt.pos
        hand.last_name = tgt.name
        hand.free_at = max(arrive, t) + tgt.use_s
    out = list(actions)
    for a, step in mode_steps:
        out.insert(out.index(a), step)
    if stats is not None:
        stats["travel"] = total_travel
    return out, issues


def place_relaxed(actions: list[Action], layout: Layout, bpm: float, kinds: set | None = None) -> None:
    """
    Give each "any time between" action (params["window"]) the moment in its window
    where it costs the least EXTRA hand travel, given the actions around it (checked beat
    by beat; ties go to the moment with the most time spare). Placed one at a time,
    each joining the fixed ones, so prep on one side groups together. In place.
    `kinds`: only place actions of these kinds (the others count as fixed where they are).
    """
    relaxed = [a for a in actions if "window" in a.params and (kinds is None or a.kind in kinds)]
    placing = {id(a) for a in relaxed}
    fixed = [a for a in actions if id(a) not in placing]
    for a in sorted(relaxed, key=lambda x: x.params["window"][1] - x.params["window"][0]):
        tgt = target_of(a, layout)
        if tgt is None:
            continue
        w0, w1 = a.params["window"]
        best = None
        t_bar = w0
        while t_bar <= w1 + 1e-9:
            t = _seconds(t_bar, bpm)
            # the fixed actions just before / after this moment (any hand)
            before = [f for f in fixed if f.set_bar <= t_bar and target_of(f, layout)]
            after = [f for f in fixed if f.set_bar > t_bar and target_of(f, layout)]
            prev = max(before, key=lambda f: f.set_bar) if before else None
            nxt = min(after, key=lambda f: f.set_bar) if after else None
            p_pos = target_of(prev, layout).pos if prev else layout.home("left" if tgt.pos[0] < 40 else "right")
            p_t = _seconds(prev.set_bar, bpm) + PRESS_S if prev else float("-inf")
            n_pos = target_of(nxt, layout).pos if nxt else p_pos
            n_t = _seconds(nxt.set_bar, bpm) if nxt else float("inf")
            t_in, t_out = travel(p_pos, tgt.pos), travel(tgt.pos, n_pos)
            spare = min(t - (p_t + t_in), n_t - (t + tgt.use_s + t_out))
            extra = t_in + t_out - travel(p_pos, n_pos)
            score = (spare < 0, round(extra, 2), -min(spare, 30))
            if best is None or score < best[0]:
                best = (score, t_bar)
            t_bar += 0.25
        a.set_bar = best[1]
        a.params["suggested"] = best[1]
        fixed.append(a)
