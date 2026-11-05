"""
Saving, loading and checking set plans.

Plans are stored as one JSON file per plan in the plans/ folder, named <id>.json.
They're plain, human-readable JSON, so you can back them up, share them or even edit
them by hand.

Deleting a plan doesn't erase it: the file is moved into plans/.trash/ so it can be
recovered.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from . import config
from .lanes import busiest_moment
from .models import MAX_LANES, Plan

# Plan ids become file names, so only allow safe characters (no slashes, dots, etc.).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PlanStore:
    """Reads and writes plan files in a folder (config.PLANS_DIR by default)."""

    def __init__(self, folder: Path = config.PLANS_DIR):
        self.folder = Path(folder)

    def _path(self, plan_id: str) -> Path:
        if not _SAFE_ID.match(plan_id or ""):
            raise ValueError(f"Invalid plan id: {plan_id!r}")
        return self.folder / f"{plan_id}.json"

    # ----- queries ----------------------------------------------------------------

    def list(self) -> list[dict]:
        """
        Short info about every saved plan, newest first:
        {id, name, updated_at, segment_count, total_bars}.
        Files that can't be read are skipped (and reported in the list as broken).
        """
        if not self.folder.exists():
            return []
        out = []
        for f in self.folder.glob("*.json"):
            try:
                plan = Plan.from_dict(json.loads(f.read_text(encoding="utf-8")))
                out.append(
                    {
                        "id": f.stem,
                        "name": plan.name,
                        "updated_at": plan.updated_at,
                        "segment_count": len(plan.segments),
                        "total_bars": plan.total_bars,
                    }
                )
            except (OSError, ValueError) as e:
                out.append({"id": f.stem, "name": f"{f.name} (unreadable: {e})",
                            "updated_at": "", "segment_count": 0, "total_bars": 0, "broken": True})
        return sorted(out, key=lambda p: p["updated_at"], reverse=True)

    def load(self, plan_id: str) -> Plan:
        """Load a plan. Raises FileNotFoundError / ValueError."""
        path = self._path(plan_id)
        if not path.exists():
            raise FileNotFoundError(f"No plan with id {plan_id}")
        plan = Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))
        plan.id = plan_id  # the file name is the source of truth
        return plan

    # ----- changes ----------------------------------------------------------------

    def save(self, plan: Plan) -> Plan:
        """
        Save a plan (creating it if it has no id yet). Sets the timestamps.
        Writes to a temporary file first, then swaps it in, so a crash mid-save can't
        leave a half-written plan.
        """
        if not plan.id:
            plan.id = uuid.uuid4().hex[:12]
        path = self._path(plan.id)
        if not plan.created_at:
            plan.created_at = _now()
        plan.updated_at = _now()

        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic on the same drive
        return plan

    def delete(self, plan_id: str) -> Path:
        """Move a plan into plans/.trash/ (not a real delete). Returns its new path."""
        path = self._path(plan_id)
        if not path.exists():
            raise FileNotFoundError(f"No plan with id {plan_id}")
        trash = self.folder / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = trash / f"{plan_id}.{stamp}.json"
        shutil.move(str(path), dest)
        return dest


# --------------------------------------------------------------------------------------
# Checking a plan against the library
# --------------------------------------------------------------------------------------


# Each warning is {"segment_id", "level", "message"}:
#   level "error"   - can't be performed as planned (e.g. a deck playing two things)
#   level "warning" - will work, but probably not what you want (e.g. silence)
#   level "info"    - worth knowing (e.g. a hard cut the solver could turn into a blend)
# segment_id None = about the whole plan.


def _w(seg_id, level: str, message: str) -> dict:
    return {"segment_id": seg_id, "level": level, "message": message}


def _name(seg) -> str:
    """How a block is referred to in messages."""
    return f"'{seg.label or 'unnamed'}' (lane {seg.deck}, set bar {seg.set_bar:g})"


def check_plan(plan: Plan, library=None) -> list[dict]:
    """
    Look for problems in a plan. Runs every time you edit the timeline, so they show
    up straight away. These are warnings, not errors: the plan still saves and loads.

    Three groups of checks:
      * Layout checks (no library needed): decks, overlaps, silence, hard cuts.
      * Track checks (need `library`, anything with get_track(id)): track still in
        Rekordbox, range inside the track, audio file present.
      * Mix checks (Phase 4, need `library`): tempo gaps, key clashes, vocal clashes
        (if the library has stem_levels), mixes starting mid-phrase. See mixing.py.
    """
    if library is None:
        return _check_layout(plan)
    return _check_layout(plan) + _check_tracks(plan, library) + _check_mixes(plan, library)


def _check_mixes(plan: Plan, library) -> list[dict]:
    from . import mixing

    get_stems = getattr(library, "stem_levels", None)
    out: list[dict] = []
    for m in mixing.find_mixes(plan):
        out += mixing.check_mix(m, library.get_track, get_stems)
    return out


def _check_layout(plan: Plan) -> list[dict]:
    out: list[dict] = []
    segs = plan.segments  # already sorted by set position

    # --- enough decks? Lanes aren't decks: lanes take turns on a deck, but at the
    # busiest moment every busy lane (playing, or its track just finished and the deck
    # is being loaded) needs a deck of its own (lanes.py) ---
    need, at, busy = busiest_moment(plan)
    if need > plan.settings.decks:
        n = plan.settings.decks
        msg = (f"At set bar {at:g}, {need} lanes need a deck at once (playing, or just "
               f"finished with no time to load the next track), but you have {n} "
               f"deck{'s' if n != 1 else ''}. Move a track later, or end one sooner.")
        for lane in busy:
            # point at the lane's block playing at that moment (or the last one before)
            on_lane = [s for s in plan.deck_segments(lane) if s.set_bar <= at] or plan.deck_segments(lane)
            out.append(_w(on_lane[-1].id, "error", msg))

    for a_i, a in enumerate(segs):
        for b in segs[a_i + 1:]:
            overlap = a.overlap_bars(b)
            # --- a deck can only play one thing at a time ---
            if a.deck == b.deck and overlap > 0:
                msg = (f"{_name(a)} and {_name(b)} overlap by {overlap:g} bars on the same lane. "
                       "A lane is one deck, which plays one thing at a time; move one to another lane.")
                out += [_w(a.id, "error", msg), _w(b.id, "error", msg)]
            # --- same track on two decks at once needs instant doubles ---
            elif (a.deck != b.deck and overlap > 0 and a.track_id == b.track_id
                  and not plan.settings.instant_doubles):
                msg = (f"'{a.track_title or a.track_id}' plays on lane {a.deck} and lane {b.deck} "
                       "at the same time. Turn on Instant doubles, or use different tracks.")
                out += [_w(a.id, "error", msg), _w(b.id, "error", msg)]

    for deck in range(1, MAX_LANES + 1):
        on_deck = plan.deck_segments(deck)
        for prev, nxt in zip(on_deck, on_deck[1:]):
            # --- loading a new track onto a deck takes time ---
            if prev.track_id != nxt.track_id and abs(nxt.set_bar - prev.set_end_bar) < 0.001:
                out.append(_w(nxt.id, "error",
                              f"Lane {deck} switches straight from '{prev.track_title}' to "
                              f"'{nxt.track_title}' with no time to load it. Put {_name(nxt)} "
                              "on another lane."))

    # --- hard cuts: a new track starts exactly as another ends, with no overlap ---
    for b in segs:
        if b.set_bar <= 1:
            continue
        # If b's deck was already playing right up to b (it just continues/jumps),
        # this isn't a track coming in, so it can't be a hard cut.
        if any(a.deck == b.deck and abs(a.set_end_bar - b.set_bar) < 0.001 for a in segs):
            continue
        ending_here = [a for a in segs if a.deck != b.deck and a.track_id != b.track_id
                       and abs(a.set_end_bar - b.set_bar) < 0.001]
        mixed = any(a.overlap_bars(b) > 0 for a in segs if a.deck != b.deck)
        if ending_here and not mixed:
            a = ending_here[0]
            out.append(_w(b.id, "info",
                          f"Hard cut from '{a.track_title}' into {_name(b)}. Drag it earlier so "
                          "the two overlap if you want a blend."))

    # --- silence: stretches of the set where no deck is playing ---
    cursor = 1.0
    for s in segs:
        if s.set_bar > cursor + 0.001:
            out.append(_w(None, "warning",
                          f"Silence from set bar {cursor:g} to {s.set_bar:g}: nothing is playing."))
        cursor = max(cursor, s.set_end_bar)
    return out


def _check_tracks(plan: Plan, library) -> list[dict]:
    out: list[dict] = []
    for seg in plan.segments:
        try:
            track = library.get_track(seg.track_id)
        except KeyError:
            out.append(_w(seg.id, "error",
                          f"{_name(seg)}: track '{seg.track_title or seg.track_id}' is no longer "
                          "in your Rekordbox library."))
            continue

        grid = track.beatgrid
        if len(grid) == 0:
            out.append(_w(seg.id, "error", f"{_name(seg)}: track has no beatgrid."))
            continue
        first_bar = grid.beat_index_to_bar(0)
        last_bar_end = grid.bar_count + 1  # position just after the final whole bar
        if seg.start_bar < first_bar:
            out.append(_w(seg.id, "error", f"{_name(seg)} starts before the beginning of the track."))
        if seg.end_bar > last_bar_end + 0.001:
            out.append(_w(seg.id, "error",
                          f"{_name(seg)} runs past the end of the track (ends at track bar "
                          f"{seg.end_bar:g}, track has {grid.bar_count} bars)."))
        if not track.summary.file_exists:
            out.append(_w(seg.id, "warning",
                          f"{_name(seg)}: audio file not found (planning works, preview won't)."))
    return out
