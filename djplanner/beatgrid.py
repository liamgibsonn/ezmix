"""
Beatgrid: converts between seconds, beat numbers and bar positions.

Rekordbox stores a track's grid as a list of beats. Each beat has a time (ms) and a
number 1-4 saying where it sits in the bar ("1" = downbeat). We keep that list and
answer questions like "what time does bar 33 start?" or "what bar is 01:23.4 in?".

Conventions (also explained in models.py):
  * "beat index" = 0-based position in the beat list (beat index 0 = first grid beat).
  * "bar position" = float, bar 1 starts at the FIRST DOWNBEAT. Each beat is 0.25 bars.
    e.g. bar 32.75 = bar 32, beat 4.
  * Beats before the first downbeat (a "pickup") get bar positions below 1.0.

Assumption: the grid is 4/4 throughout, i.e. downbeats come every 4 beats once they
start. Rekordbox grids always are, even "dynamic" ones where the tempo drifts.

This module has no Rekordbox dependency, so it's easy to test with fake grids.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Sequence

BEATS_PER_BAR = 4


class Beatgrid:
    def __init__(self, beat_times: Sequence[float], beat_numbers: Sequence[int]):
        """
        beat_times:   time of each beat in SECONDS, in ascending order.
        beat_numbers: the matching 1-4 position-in-bar from Rekordbox (1 = downbeat).
        Both lists must be the same length. An empty grid is allowed (track not
        analysed); most methods then raise ValueError.
        """
        if len(beat_times) != len(beat_numbers):
            raise ValueError("beat_times and beat_numbers must be the same length")
        self.times: list[float] = [float(t) for t in beat_times]
        self.numbers: list[int] = [int(n) for n in beat_numbers]

        # Index of the first beat marked "1". Bar 1 starts here. If the grid has no
        # downbeat at all (shouldn't happen), treat the first beat as the downbeat.
        self.first_downbeat_index: int = next(
            (i for i, n in enumerate(self.numbers) if n == 1), 0
        )

    # ----- basic info -------------------------------------------------------------

    def __len__(self) -> int:
        """Number of beats in the grid."""
        return len(self.times)

    @property
    def bar_count(self) -> int:
        """Number of COMPLETE bars from bar 1 to the end of the grid."""
        beats_from_bar1 = len(self.times) - self.first_downbeat_index
        return max(0, beats_from_bar1 // BEATS_PER_BAR)

    def _require_beats(self) -> None:
        if not self.times:
            raise ValueError("this track has no beatgrid (not analysed?)")

    def _edge_interval(self, at_end: bool) -> float:
        """
        Length of one beat (seconds) at the start or end of the grid. Used to
        extrapolate past the edges. Falls back to 0.5 s (120 BPM) on a 1-beat grid.
        """
        if len(self.times) < 2:
            return 0.5
        if at_end:
            return self.times[-1] - self.times[-2]
        return self.times[1] - self.times[0]

    # ----- beat index <-> bar position ----------------------------------------------

    def beat_index_to_bar(self, beat_index: float) -> float:
        """0-based beat index (can be fractional) -> bar position."""
        return (beat_index - self.first_downbeat_index) / BEATS_PER_BAR + 1.0

    def bar_to_beat_index(self, bar: float) -> float:
        """Bar position -> 0-based beat index (may be fractional or off the grid)."""
        return (bar - 1.0) * BEATS_PER_BAR + self.first_downbeat_index

    # ----- beat index <-> seconds ---------------------------------------------------

    def time_at_beat_index(self, beat_index: float) -> float:
        """
        Seconds at a (possibly fractional) beat index. Between two beats we
        interpolate linearly. Outside the grid we carry on at the tempo of the
        first/last beat, so positions slightly past the ends still make sense.
        """
        self._require_beats()
        last = len(self.times) - 1
        if beat_index <= 0:
            return self.times[0] + beat_index * self._edge_interval(at_end=False)
        if beat_index >= last:
            return self.times[last] + (beat_index - last) * self._edge_interval(at_end=True)
        i = int(beat_index)  # beat just before the position
        frac = beat_index - i  # how far towards the next beat (0..1)
        return self.times[i] + frac * (self.times[i + 1] - self.times[i])

    def beat_index_at_time(self, seconds: float) -> float:
        """Inverse of time_at_beat_index: seconds -> fractional beat index."""
        self._require_beats()
        last = len(self.times) - 1
        if seconds <= self.times[0]:
            return (seconds - self.times[0]) / self._edge_interval(at_end=False)
        if seconds >= self.times[last]:
            return last + (seconds - self.times[last]) / self._edge_interval(at_end=True)
        # bisect finds the first beat AFTER `seconds`; the one before it is our beat.
        i = bisect_right(self.times, seconds) - 1
        span = self.times[i + 1] - self.times[i]
        return i + (seconds - self.times[i]) / span

    # ----- bar position <-> seconds (what the rest of the app mostly uses) --------

    def bar_to_time(self, bar: float) -> float:
        """Bar position -> seconds. e.g. bar_to_time(33) = when bar 33 starts."""
        return self.time_at_beat_index(self.bar_to_beat_index(bar))

    def time_to_bar(self, seconds: float) -> float:
        """Seconds -> bar position. e.g. 32.75 means bar 32, beat 4."""
        return self.beat_index_to_bar(self.beat_index_at_time(seconds))

    # ----- helpers for human-readable output --------------------------------------

    @staticmethod
    def bar_and_beat(bar_position: float) -> tuple[int, int]:
        """
        Split a bar position into (bar number, beat 1-4), rounding to the nearest
        beat. Used for cheat-sheet text like "Bar 32, beat 4".
        e.g. 32.75 -> (32, 4); 33.0 -> (33, 1).
        """
        total_beats = round((bar_position - 1.0) * BEATS_PER_BAR)
        bar, beat = divmod(total_beats, BEATS_PER_BAR)
        return bar + 1, beat + 1

    def bpm_at_bar(self, bar: float) -> float:
        """Local tempo (BPM) at a bar position, from the gap between beats there."""
        self._require_beats()
        i = min(max(int(self.bar_to_beat_index(bar)), 0), len(self.times) - 2)
        if i < 0:  # 1-beat grid
            return 0.0
        return 60.0 / (self.times[i + 1] - self.times[i])

    # ----- constructors -----------------------------------------------------------

    @classmethod
    def constant(
        cls, bpm: float, bars: int, offset_s: float = 0.0, pickup_beats: int = 0
    ) -> "Beatgrid":
        """
        Build a perfectly steady 4/4 grid. Used by the tests (fake tracks).
        pickup_beats = how many beats come before the first downbeat.
        """
        beat_len = 60.0 / bpm
        n = bars * BEATS_PER_BAR + pickup_beats
        times = [offset_s + i * beat_len for i in range(n)]
        # Numbering: the pickup beats count up to 4 so beat `pickup_beats` lands on "1".
        numbers = [((i - pickup_beats) % BEATS_PER_BAR) + 1 for i in range(n)]
        return cls(times, numbers)
