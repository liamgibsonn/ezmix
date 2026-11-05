"""
Phase 4: mixes between decks (CHECKS only).

Wherever blocks on two different decks overlap on the timeline, that's a MIX: one track
(the "outgoing" one, which was playing first) hands over to another (the "incoming" one).
HOW you mix (fade, cut, echo, backspin, bass swap...) is YOUR choice, so this module
doesn't write mixing steps (the user, 2026-10-15). What it does:

  * finds the mixes (find_mixes), for the timeline and the checks
  * CHECKS each one (shown on the incoming block):
       - tempo gap: BPMs more than TEMPO_GAP_WARN apart (half/double time counts as close)
       - key clash: keys not neighbours on the Camelot wheel
       - vocal clash: both tracks have vocals during the overlap (needs stem analysis)
       - off-phrase: the incoming track starts part-way through a phrase of the outgoing

Getting every track to the right place at the right time (pre-rolls, cues, jumps,
two hands) is the solver's job: djplanner/solver.py.

Stem levels ("stems"): per-bar loudness of each part of a track (drums, bass, other =
synths etc., vocals), from djplanner/stems.py. Optional: without them the vocal check
is skipped.

Pure logic (no web, Rekordbox or audio code), tested in tests/test_mixing.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .models import Plan, Segment, Track

# ---- settings (edit these to change how mixes are planned / checked) ----------------

# Tempo difference that gets a warning (0.06 = 6%). Half / double time (e.g. 87 vs 174)
# counts as the same tempo.
TEMPO_GAP_WARN = 0.06
# A stem counts as "playing" in a bar if it's within this many dB of that stem's loud
# parts in the track (its 90th-percentile bar). Vocals that are really there sit at about
# -3 dB on this scale; "no vocals" is -20 dB or lower.
STEM_PRESENT_DB = -12.0
# ...and a stem that's this far below the track's LOUDEST stem is silent even at its own
# loudest (an instrumental track's "vocals" stem is just faint leftovers).
STEM_SILENT_BELOW_DB = 25.0
# Vocals from both tracks at once for at least this many bars = a vocal clash.
VOCAL_CLASH_MIN_BARS = 2
# Phrases are counted in blocks of this many bars when checking a mix starts on one.
PHRASE_BARS = 8


StemLookup = Callable[[str], Optional[dict]]  # track_id -> {stem: [dB per bar]} or None


@dataclass
class Mix:
    """
    Two decks playing at once. `out_blocks` = the outgoing deck's run of blocks (one
    track played continuously, maybe with jumps between blocks), `in_blocks` = the
    incoming deck's run. Only the overlapping part (start..end) is the mix.
    """

    out_blocks: list[Segment]
    in_blocks: list[Segment]
    start: float  # set bar where the overlap starts
    end: float  # set bar just after it ends
    # True when the incoming run also ENDS before the outgoing one: it's a LAYER over a
    # track that keeps playing (no handover, so no bass swap).
    layer: bool = False

    @property
    def bars(self) -> float:
        return self.end - self.start

    @staticmethod
    def _block_at(blocks: list[Segment], set_bar: float) -> Segment:
        for b in blocks:
            if b.set_bar <= set_bar + 1e-9 < b.set_end_bar:
                return b
        return blocks[-1] if set_bar >= blocks[-1].set_bar else blocks[0]

    @property
    def out_seg(self) -> Segment:
        """The outgoing block playing when the mix starts."""
        return self._block_at(self.out_blocks, self.start)

    @property
    def in_seg(self) -> Segment:
        """The incoming block playing when the mix starts."""
        return self._block_at(self.in_blocks, self.start)

    def out_track_bar(self, set_bar: float) -> float:
        """Where the outgoing track is (track bar) at a set bar."""
        b = self._block_at(self.out_blocks, set_bar)
        return b.start_bar + (set_bar - b.set_bar)

    def in_track_bar(self, set_bar: float) -> float:
        b = self._block_at(self.in_blocks, set_bar)
        return b.start_bar + (set_bar - b.set_bar)


def find_mixes(plan: Plan) -> list[Mix]:
    """
    Every overlap between two decks' runs (a run = one track played continuously on a
    deck: back-to-back blocks of the same track), in set order. The run that started
    first is the outgoing one (if both start together, the lower deck).
    """
    from .solver import split_into_runs  # (solver imports this module too)

    runs = split_into_runs(plan)
    out: list[Mix] = []
    for k, a in enumerate(runs):
        for b in runs[k + 1:]:
            if a.deck == b.deck:
                continue
            start, end = max(a.set_start, b.set_start), min(a.set_end, b.set_end)
            if end - start <= 1e-9:
                continue
            first, second = sorted((a, b), key=lambda r: (r.set_start, r.deck))
            layer = first.set_end > second.set_end + 1e-9
            out.append(Mix(first.blocks, second.blocks, start, end, layer))
    return sorted(out, key=lambda m: (m.start, m.in_seg.deck))


# ----- stems helpers ----------------------------------------------------------------


def _stem_present(stems: Optional[dict], stem: str, track_bar: float) -> Optional[bool]:
    """
    Is this stem playing in this bar of the track? None if unknown. "Playing" = within
    STEM_PRESENT_DB of the stem's own loud parts, where those loud parts count as at
    least the track's loudest stem minus STEM_SILENT_BELOW_DB (so a stem that's silent
    all the way through never counts as playing).
    """
    if not stems or stem not in stems or not len(stems[stem]):
        return None
    levels = np.asarray(stems[stem], dtype=float)
    i = int(np.floor(track_bar)) - 1
    if not 0 <= i < len(levels):
        return None
    loudest = max(np.percentile(np.asarray(v, dtype=float), 90) for v in stems.values() if len(v))
    ref = max(np.percentile(levels, 90), loudest - STEM_SILENT_BELOW_DB)
    return bool(levels[i] - ref >= STEM_PRESENT_DB)


def _stem_bars(stems: Optional[dict], stem: str, track_bar_at: Callable[[float], float],
               start: float, n: int) -> Optional[list[bool]]:
    """Present / not for n set bars from `start`. None if unknown."""
    vals = [_stem_present(stems, stem, track_bar_at(start + k)) for k in range(n)]
    return None if any(v is None for v in vals) else vals


# ----- checks -----------------------------------------------------------------------


def tempo_gap(bpm_a: float, bpm_b: float) -> Optional[float]:
    """Relative tempo difference (0.05 = 5%), treating half / double time as equal."""
    if not bpm_a or not bpm_b:
        return None
    r = bpm_b / bpm_a
    return min(abs(r / k - 1) for k in (0.5, 1.0, 2.0))


def camelot_compatible(a: str, b: str) -> Optional[bool]:
    """Do two Camelot keys mix (same, +-1 on the wheel, or relative major/minor)?"""
    if not a or not b:
        return None
    try:
        na, la = int(a[:-1]), a[-1].upper()
        nb, lb = int(b[:-1]), b[-1].upper()
    except ValueError:
        return None
    if la == lb:
        return (na - nb) % 12 in (0, 1, 11)
    return na == nb


def _phrase_offset(track: Optional[Track], track_bar: float) -> Optional[float]:
    """Bars into the current phrase-block at track_bar (0 = on a phrase line), or None."""
    if track is None or not track.phrases:
        return None
    starts = sorted(p.start_bar for p in track.phrases if p.start_bar <= track_bar + 1e-6)
    if not starts:
        return None
    return (track_bar - starts[-1]) % PHRASE_BARS


def check_mix(mix: Mix, get_track: Callable[[str], Track], get_stems: Optional[StemLookup] = None) -> list[dict]:
    """Warnings for one mix: [{segment_id, level, message}]."""
    out: list[dict] = []
    o, i = mix.out_seg, mix.in_seg
    names = f"'{o.track_title or o.track_id}' → '{i.track_title or i.track_id}'"

    def w(level, msg):
        out.append({"segment_id": i.id, "level": level, "message": msg})

    def track(tid):
        try:
            return get_track(tid)
        except KeyError:
            return None

    to, ti = track(o.track_id), track(i.track_id)

    # --- tempo ---
    bpm_o = (to.summary.bpm if to else 0) or o.bpm
    bpm_i = (ti.summary.bpm if ti else 0) or i.bpm
    gap = tempo_gap(bpm_o, bpm_i)
    if gap is not None and gap > TEMPO_GAP_WARN:
        w("warning", f"Tempo gap {names}: {bpm_o:g} → {bpm_i:g} BPM ({gap:.0%} apart). "
                     "Synced, one of them will sound noticeably sped up or slowed down.")

    # --- key ---
    if to and ti:
        ok = camelot_compatible(to.summary.camelot, ti.summary.camelot)
        if ok is False:
            w("warning", f"Key clash {names}: {to.summary.camelot} ({to.summary.key}) and "
                         f"{ti.summary.camelot} ({ti.summary.key}) aren't neighbours on the Camelot "
                         "wheel. Keep the overlap short or cut one track's melody (EQ / filter).")

    # --- vocals at the same time (needs stems for both) ---
    if get_stems is not None and mix.bars >= VOCAL_CLASH_MIN_BARS:
        so, si = get_stems(o.track_id), get_stems(i.track_id)
        n = int(np.ceil(mix.bars))
        vo = _stem_bars(so, "vocals", mix.out_track_bar, mix.start, n)
        vi = _stem_bars(si, "vocals", mix.in_track_bar, mix.start, n)
        if vo is not None and vi is not None:
            both = [k for k in range(n) if vo[k] and vi[k]]
            if len(both) >= VOCAL_CLASH_MIN_BARS:
                w("warning", f"Vocal clash {names}: both have vocals for {len(both)} of the "
                             f"{mix.bars:g} overlapping bars (from set bar {mix.start + both[0]:g}). "
                             "Shorten the overlap or move it to an instrumental part.")

    # --- does the incoming track come in on a phrase line of the outgoing one? ---
    off = _phrase_offset(to, mix.out_track_bar(mix.start))
    if off is not None and min(off, PHRASE_BARS - off) > 0.01:
        w("info", f"Deck {i.deck} comes in {off:g} bar{'s' if off != 1 else ''} into a phrase of "
                  f"'{o.track_title or o.track_id}'. Mixes usually sound best starting on a phrase line.")
    return out
