"""
Turns a solved plan into a cheat sheet you can follow while playing:

  PREP (before the set)
    Track A on Deck 1: set hot cue C at bar 33 "→ Chorus 2"; memory cue at bar 17
  STEPS
    Bar 1, beat 1    Deck 1  Press PLAY (…)
    Bar 16, beat 4   Deck 1  Hit pad C on beat 4 (→ track bar 33, 'Chorus 2')
    Bar 25, beat 1   Deck 2  Hit pad A to start 'Intro'

Positions are SET bars (counted from the start of the set). Clock times are rough:
they assume the whole set runs at the first block's BPM (decks synced to it).

EASY TO READ (the user): each step is a SHORT instruction you can take in at a glance
("PLAY (fader down)"), with the details on a small line under it ("4 bars early ·
comes in at 53.1"), and the bar first. Bars are written "bar.beat" everywhere.

GET-READY GROUPS (Phase 4): the prep steps for each track that comes in with a fader
move (load, go to the cue, deck switch, PLAY with the fader down, a muted beat jump)
are marked as one group, drawn in that deck's colour, so you can see what belongs to
what.
"""

from __future__ import annotations

from typing import Callable

from .beatgrid import Beatgrid
from .models import Plan, Track
from .solver import Solution


def _clock(set_bar: float, bpm: float) -> str:
    """Set bar -> "m:ss" from the start of the set (blank if BPM unknown)."""
    if not bpm:
        return ""
    s = round((set_bar - 1) * 4 * 60 / bpm)
    return f"{s // 60}:{s % 60:02d}"


def build_cheatsheet(plan: Plan, solution: Solution, get_track: Callable[[str], Track]) -> dict:
    """
    Structured cheat sheet (the web page and the text download are both made from
    this). Returns {name, bpm, total_bars, duration, setup, decks, steps, groups, issues}.
    `decks` = which tracks go on which deck, in order (shown at the top instead of
    "Load ..." steps: the DJ decides when to load, the user).
    """
    bpm = plan.segments[0].bpm if plan.segments else 0.0

    def title(track_id: str) -> str:
        try:
            return get_track(track_id).summary.title
        except KeyError:
            seg = next((s for s in plan.segments if s.track_id == track_id), None)
            return (seg.track_title if seg else "") or track_id

    # (No "prep before the set" list: this is for one transition at a time, done on the
    # fly (the user). Steps that need a cue you don't have say how to make it there and
    # then.)

    # --- steps: every action in time order ---
    steps = []
    for a in solution.actions:
        bar, beat = Beatgrid.bar_and_beat(a.set_bar)
        window = a.params.get("window")
        short, detail = describe(a)
        steps.append({
            "short": short,
            "detail": detail,
            "set_bar": a.set_bar,
            "bar": bar,
            "beat": beat,
            "time": _clock(a.set_bar, bpm),
            "deck": a.deck,
            "kind": a.kind,
            "pad": a.pad,
            "text": a.text,
            "flags": a.flags,
            # loads and deck switches can happen any time before; relaxed actions any
            # time in their window; everything else is "on the beat"
            "when": "between" if window else "by" if a.kind in BEFORE_KINDS else "at",
            "window": [Beatgrid.bar_and_beat(window[0])[0], Beatgrid.bar_and_beat(window[1])[0]] if window else None,
            # "any time" steps are shown as "before <the end of their window>" (the user)
            "before": bb(window[1]) if window else None,
            "group": None,
        })

    groups = _get_ready_groups(plan, solution, steps, title)

    # List the steps in order of the bar SHOWN for them, so the column always counts up:
    # an "any time" step is shown (and listed) at the bar it must be done before. At the
    # same bar: getting ready (load, cue, deck switch, pad mode) before doing.
    def shown_at(i):
        a = solution.actions[i]
        w = a.params.get("window")
        return (w[1] if w else a.set_bar, SHOW_ORDER.get(a.kind, 9), i)

    # (loading isn't a step: it's in the "Tracks on each deck" list at the top)
    order = sorted((i for i in range(len(steps)) if steps[i]["kind"] != "load"), key=shown_at)
    new_index = {old: new for new, old in enumerate(order)}
    steps = [steps[i] for i in order]
    for g in groups:
        g["steps"] = sorted(new_index[i] for i in g["steps"] if i in new_index)

    # --- which tracks go on which deck, in the order they're used ---
    decks = []
    for d in sorted({s.deck for s in plan.segments}):
        tracks = []
        for s in plan.deck_segments(d):
            name = title(s.track_id)  # (the library's name; falls back to the plan's copy)
            if not tracks or tracks[-1]["title"] != name:
                tracks.append({"title": name, "from_bar": bb(s.set_bar)})
        side = (plan.settings.jog_name(plan.settings.jog_of(d)) if plan.settings.shared_jogs else "")
        decks.append({"deck": d, "side": side, "tracks": tracks})

    end = max((s.set_end_bar for s in plan.segments), default=1.0)
    return {
        "name": plan.name,
        "bpm": bpm,
        "total_bars": end - 1,
        "duration": _clock(end, bpm),
        "cost": round(solution.cost, 2),
        "setup": setup_text(plan),
        "decks": decks,
        "steps": steps,
        "groups": groups,
        "issues": solution.issues,
    }


PREP_KINDS = {"load", "cue", "deck_switch", "pad_mode", "start", "beat_jump"}
# Same shown bar: get ready first (load, cue, switch the jog wheel, pad mode), then act
SHOW_ORDER = {"load": 0, "cue": 1, "deck_switch": 2, "pad_mode": 3}
# Steps you can do any time BEFORE their bar (no exact timing)
BEFORE_KINDS = {"load", "cue", "deck_switch", "pad_mode"}


def bb(set_bar: float) -> str:
    """Set bar position as the cheat sheet writes it: "bar.beat", e.g. 52.3."""
    bar, beat = Beatgrid.bar_and_beat(set_bar)
    return f"{bar}.{beat}"


def describe(a) -> tuple[str, str]:
    """
    (short instruction, detail line) for one step, e.g. ("PLAY (fader down)",
    "4 bars early · comes in at 53.1"). The short part is what you do; the detail is
    why / where it lands. Falls back to the action's full text.
    """
    p = a.params
    label = p.get("label", "")
    k = a.kind
    if k == "load":
        return f"Load '{p.get('title', a.track_id)}'", ""
    if k == "cue":
        if p.get("new_cue"):
            return f"Find track bar {a.track_bar:g}, press CUE", "it waits there, ready to PLAY"
        return f"Go to your memory cue at track bar {a.track_bar:g}", "press CUE so it sits there, ready"
    if k == "pad_mode":
        extra = "hold SHIFT + press BEAT JUMP" if p.get("mode") == "beat_loop" else ""
        names = {"hot_cue": "HOT CUE", "beat_jump": "BEAT JUMP", "beat_loop": "BEAT LOOP"}
        return f"Pads → {names.get(p.get('mode'), p.get('mode'))} mode", extra
    if k == "deck_switch":
        side = "Left" if p.get("jog") == 1 else "Right" if p.get("jog") == 2 else f"Jog {p.get('jog')}"
        return f"{side} side → Deck {a.deck}", "press its DECK button"
    new_pad = (f"first set pad {a.pad} at track bar {a.track_bar:g} · "
               if p.get("new_cue") and a.pad else "")
    if k == "start":
        what = f"Hit pad {a.pad}" if a.pad else "PLAY"
        if p.get("preroll"):
            n = p["preroll"]
            return (f"{what} (fader DOWN)",
                    new_pad + f"{n:g} bar{'s' if n != 1 else ''} early · '{label}' comes in at {bb(p['comes_in'])}")
        return what, new_pad + f"'{label}' starts"
    if k == "fader_up":
        return "Fader UP", f"'{label}' comes in" + (" · it's been playing muted" if p.get("reentry") else "")
    if k == "fader_down":
        return "Fader DOWN · keep it playing", f"it comes back at {bb(p['comes_back'])}" if "comes_back" in p else ""
    if k == "stop":
        return "Fader DOWN / stop", f"end of '{label}'" if label else ""
    if k == "beat_jump":
        beats = sum(p.get("beats", []))
        bars = abs(beats) / 4
        size = f"{bars:g} bar{'s' if bars != 1 else ''}" if beats % 4 == 0 else f"{abs(beats)} beats"
        short = f"Beat jump {'back' if beats < 0 else 'forward'} {size}"
        if p.get("presses", 1) > 1:
            short += f" ({' then '.join(f'{b:+d}' for b in p['beats'])} beats)"
        if p.get("reentry"):
            return short, f"while muted, any time · it'll reach '{label}' (track bar {a.track_bar:g}) at {bb(p['comes_in'])}"
        return short, f"on the 1 → '{label}' (track bar {a.track_bar:g})"
    if k == "hot_cue":
        return f"Hit pad {a.pad or '?'}", new_pad + f"on beat 4 → '{label}' (track bar {a.track_bar:g})"
    if k == "loop_in":
        if p.get("method") == "auto":
            return f"{p.get('beats')}-beat loop", f"{p.get('passes')} times round · '{label}'"
        return "LOOP IN", f"on the 1 (track bar {a.track_bar:g})"
    if k == "loop_out":
        return "LOOP OUT", f"on beat 4 · loops {p.get('loop_bars', 0):g} bars"
    if k == "loop_exit":
        return "Loop EXIT", "during the last time round"
    if k == "render":
        return "Can't be played live", a.text
    return a.text, ""


def _get_ready_groups(plan: Plan, solution: Solution, steps: list[dict], title) -> list[dict]:
    """
    One group per track coming in with a FADER UP: the prep steps for it (same block,
    done before it comes in) plus the fader up itself. Marks each step's "group" with
    the group's index. Returns [{title, deadline, while, steps: [step indexes]}].
    """
    groups = []
    acts = solution.actions
    for k, up in enumerate(acts):
        if up.kind != "fader_up":
            continue
        members = [i for i, a in enumerate(acts)
                   if a.segment_id == up.segment_id and a.kind in PREP_KINDS and a.set_bar <= up.set_bar]
        if not members:
            continue  # nothing to get ready (it was just faded down and up again)
        members.append(k)
        start = min(acts[i].set_bar for i in members)
        # what you can hear while getting it ready: other decks' blocks in that stretch
        playing = []
        for s in plan.segments:
            if s.deck != up.deck and s.set_bar < up.set_bar and s.set_end_bar > start:
                name = title(s.track_id)  # (the library's name; falls back to the plan's copy)
                if name not in playing:
                    playing.append(name)
        g = len(groups)
        for i in members:
            if steps[i]["group"] is None:
                steps[i]["group"] = g
        groups.append({
            "track": title(up.track_id),
            "deck": up.deck,
            "deadline": up.set_bar,
            "while": playing,
            "steps": sorted(members),
        })
    return groups


def setup_text(plan: Plan) -> str:
    """
    One line describing the deck setup, e.g.
    "4 tracks on 2 jog wheels: Left = Decks 1 & 3, Right = Decks 2 & 4".
    ("tracks" = how many can play at once, as the timeline's dropdown calls it.)
    """
    st = plan.settings
    decks = f"{st.decks} track{'s' if st.decks != 1 else ''}"
    if not st.shared_jogs:
        return f"{decks}, one jog wheel each"
    sides = []
    for jog in range(1, st.jog_wheels + 1):
        on = [str(d) for d in range(1, st.decks + 1) if st.jog_of(d) == jog]
        label = "Deck" if len(on) == 1 else "Decks"
        sides.append(f"{st.jog_name(jog)} = {label} {' & '.join(on)}")
    return f"{decks} on {st.jog_wheels} jog wheel{'s' if st.jog_wheels != 1 else ''}: " + ", ".join(sides)


def cheatsheet_text(sheet: dict) -> str:
    """Plain-text version, for printing or reading on a phone."""
    lines = [sheet["name"], "=" * len(sheet["name"])]
    if sheet["bpm"]:
        lines.append(f"{sheet['total_bars']:g} bars at {sheet['bpm']:g} BPM")
    if sheet.get("setup"):
        lines.append(sheet["setup"])
    lines.append("")

    if sheet["issues"]:
        lines.append("PROBLEMS")
        for i in sheet["issues"]:
            lines.append(f"  [{i['level']}] {i['message']}")
        lines.append("")

    lines.append("TRACKS ON EACH DECK")
    for d in sheet.get("decks", []):
        side = f" ({d['side']})" if d["side"] else ""
        names = ", then ".join(f"{t['title']} (from {t['from_bar']})" if k else t["title"]
                               for k, t in enumerate(d["tracks"]))
        lines.append(f"  Deck {d['deck']}{side}: {names}")
    lines.append("")

    lines.append("STEPS   (bar.beat  ·  deck  ·  what to do  ·  details; all in bars)")
    for s in sheet["steps"]:
        if s["when"] == "by" and s["set_bar"] <= 1:
            when = ""  # getting ready before anything starts: obvious, no bar number (the user)
        elif s["when"] == "between" and s.get("before"):
            when = f"by {s['before']}"
        else:
            when = f"{'by ' if s['when'] == 'by' else ''}{s['bar']}.{s['beat']}"
        flag = "  ⚠ TOO TIGHT" if "awkward" in s["flags"] else "  (busy)" if "busy" in s["flags"] else ""
        mark = "|" if s.get("group") is not None else " "
        detail = f"   - {s['detail']}" if s.get("detail") else ""
        lines.append(f"  {mark} {when:<9} Deck {s['deck']}  {s.get('short') or s['text']}{flag}{detail}")
    return "\n".join(lines) + "\n"
