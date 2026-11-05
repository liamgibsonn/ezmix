"""
Turns raw Rekordbox analysis values into our models (Beatgrid, Phrase, Cue, waveform).

Everything here is a pure function taking plain numbers/lists, with no file or database
access, so it can be tested with fake data. rekordbox_reader.py does the reading and
calls these.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from .beatgrid import Beatgrid
from .models import Cue, Phrase

# --------------------------------------------------------------------------------------
# Beatgrid
# --------------------------------------------------------------------------------------


def build_beatgrid(entries: Iterable[Mapping[str, Any]]) -> Beatgrid:
    """
    Build a Beatgrid from Rekordbox PQTZ entries. Each entry has:
      "time": milliseconds from track start, "beat": 1-4 position in bar.
    (Also "tempo" = BPM*100, which we don't need: tempo comes from beat spacing.)
    """
    times, numbers = [], []
    for e in entries:
        times.append(e["time"] / 1000.0)
        numbers.append(e["beat"])
    return Beatgrid(times, numbers)


# --------------------------------------------------------------------------------------
# Phrases
# --------------------------------------------------------------------------------------

# Rekordbox analyses each track with a "mood" that decides what the phrase kind numbers
# mean. (Format documented by the Deep Symmetry "crate digger" project.)
MOOD_HIGH, MOOD_MID, MOOD_LOW = 1, 2, 3

# For each mood: kind number -> (category, label).
# Category = what the solver/colours care about; label = what you see.
PHRASE_KINDS: dict[int, dict[int, tuple[str, str]]] = {
    MOOD_HIGH: {  # typical for dance music
        1: ("intro", "Intro"),
        2: ("up", "Up"),
        3: ("down", "Down"),
        5: ("chorus", "Chorus"),
        6: ("outro", "Outro"),
    },
    MOOD_MID: {
        1: ("intro", "Intro"),
        2: ("verse", "Verse 1"),
        3: ("verse", "Verse 2"),
        4: ("verse", "Verse 3"),
        5: ("verse", "Verse 4"),
        6: ("verse", "Verse 5"),
        7: ("verse", "Verse 6"),
        8: ("bridge", "Bridge"),
        9: ("chorus", "Chorus"),
        10: ("outro", "Outro"),
    },
    MOOD_LOW: {
        1: ("intro", "Intro"),
        2: ("verse", "Verse 1"),
        3: ("verse", "Verse 1"),
        4: ("verse", "Verse 1"),
        5: ("verse", "Verse 2"),
        6: ("verse", "Verse 2"),
        7: ("verse", "Verse 2"),
        8: ("bridge", "Bridge"),
        9: ("chorus", "Chorus"),
        10: ("outro", "Outro"),
    },
}


def parse_phrases(
    mood: int,
    entries: Sequence[Mapping[str, Any]],
    end_beat: int,
    grid: Beatgrid,
) -> list[Phrase]:
    """
    Convert Rekordbox phrase analysis (the PSSI tag) into Phrase objects.

    mood:     1/2/3 (high/mid/low), selects the kind-number table above.
    entries:  one per phrase, each with
                "beat": 1-based beat NUMBER where the phrase starts (beat 1 = first
                        beat of the grid, so beat index = beat - 1),
                "kind": phrase kind number (meaning depends on mood),
                "fill": non-zero if the phrase ends with a fill-in,
                "beat_fill": beat number where that fill starts.
    end_beat: beat number where the last phrase ends.
    grid:     the track's beatgrid, used to turn beat numbers into bar positions.
    """
    kinds = PHRASE_KINDS.get(mood, PHRASE_KINDS[MOOD_HIGH])
    ordered = sorted(entries, key=lambda e: e["beat"])
    phrases: list[Phrase] = []

    for i, e in enumerate(ordered):
        start_beat = e["beat"]
        # A phrase runs until the next one starts; the last one runs to end_beat.
        next_beat = ordered[i + 1]["beat"] if i + 1 < len(ordered) else end_beat
        if next_beat <= start_beat:
            continue  # zero-length/corrupt entry; skip rather than crash

        start_bar = grid.beat_index_to_bar(start_beat - 1)
        length_bars = (next_beat - start_beat) / 4.0
        category, label = kinds.get(e["kind"], ("unknown", f"Phrase {e['kind']}"))

        fill_bar: Optional[float] = None
        if e.get("fill") and e.get("beat_fill"):
            fill_bar = grid.beat_index_to_bar(e["beat_fill"] - 1)

        phrases.append(
            Phrase(
                kind=category,
                label=label,
                start_bar=start_bar,
                length_bars=length_bars,
                source="rekordbox",
                fill_start_bar=fill_bar,
            )
        )

    number_repeated_labels(phrases)
    return phrases


def number_repeated_labels(phrases: list[Phrase]) -> None:
    """
    Make labels unique so you (and cue names like "→ Chorus 2") can tell them apart:
    if "Chorus" appears 3 times they become "Chorus 1", "Chorus 2", "Chorus 3".
    Labels that already end in a number ("Verse 2") get a letter instead
    ("Verse 2a", "Verse 2b"). Modifies the list in place.
    """
    totals: dict[str, int] = {}
    for p in phrases:
        totals[p.label] = totals.get(p.label, 0) + 1

    seen: dict[str, int] = {}
    for p in phrases:
        base = p.label
        if totals[base] < 2:
            continue
        seen[base] = seen.get(base, 0) + 1
        n = seen[base]
        if base[-1].isdigit():
            p.label = f"{base}{chr(ord('a') + n - 1)}"
        else:
            p.label = f"{base} {n}"


def fallback_phrases(grid: Beatgrid, block_bars: int) -> list[Phrase]:
    """
    For tracks WITHOUT phrase analysis: split the track into plain blocks of
    `block_bars` bars from bar 1 (last block may be shorter). Gives you something to
    drag around even if Rekordbox didn't analyse the structure.
    """
    phrases = []
    total = grid.bar_count
    start = 1
    while start <= total:
        length = min(block_bars, total - start + 1)
        end = start + length - 1
        phrases.append(
            Phrase(
                kind="bars",
                label=f"Bars {start}-{end}",
                start_bar=float(start),
                length_bars=float(length),
                source="auto",
            )
        )
        start += block_bars
    return phrases


# --------------------------------------------------------------------------------------
# Cues
# --------------------------------------------------------------------------------------

# Rekordbox's database stores which pad a hot cue is on as a "Kind" number. 0 = memory
# cue. 1 = pad A and 2 = pad B are confirmed on this library; the rest follow the
# obvious pattern but are UNVERIFIED. If a cue ever shows on the wrong pad, fix it here.
# Kinds not in this table are reported with pad=None and treated as "taken".
HOT_CUE_KIND_TO_PAD: dict[int, str] = {
    1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F", 7: "G", 8: "H",
}


def build_cue(
    kind: int,
    in_ms: float,
    out_ms: Optional[float],
    comment: Optional[str],
    color: Optional[int],
    grid: Optional[Beatgrid],
) -> Cue:
    """
    Build a Cue from one row of Rekordbox's cue table.
    out_ms is the loop end (or -1/None if this cue isn't a loop).
    grid may be None/empty for un-analysed tracks; bar positions are then 0.
    """
    time_s = in_ms / 1000.0
    loop_end_s = out_ms / 1000.0 if out_ms is not None and out_ms > 0 else None
    has_grid = grid is not None and len(grid) > 0

    return Cue(
        type="memory" if kind == 0 else "hot",
        pad=None if kind == 0 else HOT_CUE_KIND_TO_PAD.get(kind),
        raw_kind=kind,
        time_s=time_s,
        bar=grid.time_to_bar(time_s) if has_grid else 0.0,
        loop_end_s=loop_end_s,
        loop_end_bar=grid.time_to_bar(loop_end_s) if has_grid and loop_end_s else None,
        comment=comment or "",
        color=color if color is not None and color >= 0 else None,
    )


# --------------------------------------------------------------------------------------
# Waveform
# --------------------------------------------------------------------------------------

# Rekordbox's detailed mono waveform (PWV3 tag) has 150 columns per second, each with a
# height of 0-31.
WAVEFORM_COLUMNS_PER_SECOND = 150
WAVEFORM_MAX_HEIGHT = 31


def waveform_per_beat(heights: Sequence[int], grid: Beatgrid) -> list[float]:
    """
    Squash the detailed waveform down to ONE loudness value per beat (0.0-1.0, the peak
    within that beat). This lines the waveform up with bars, which is how the UI draws
    the track, and keeps the data small.
    """
    if not heights or len(grid) == 0:
        return []
    n = len(heights)
    out = []
    for i in range(len(grid)):
        start_t = grid.times[i]
        end_t = grid.times[i + 1] if i + 1 < len(grid) else start_t + grid._edge_interval(True)
        a = max(0, int(start_t * WAVEFORM_COLUMNS_PER_SECOND))
        b = min(n, max(a + 1, int(end_t * WAVEFORM_COLUMNS_PER_SECOND)))
        if a >= n:
            out.append(0.0)
            continue
        out.append(max(heights[a:b]) / WAVEFORM_MAX_HEIGHT)
    return out
