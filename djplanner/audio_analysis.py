"""
Our own phrase detection, tuned for EDM (Intro / Build / Drop / Breakdown / Outro).

Rekordbox's phrase analysis uses song-structure names (Verse, Chorus, Bridge...) and
often splits drops oddly. This module listens to the track itself:

  1. DECODE   ffmpeg decodes the audio to mono samples in memory (the file is only
              read, never changed).
  2. MEASURE  for every bar on the beatgrid: loudness, sub/kick bass, mids, highs, and a
              rough "sound fingerprint" (energy in 12 frequency bands).
  3. SPLIT    phrases in dance music change on 4/8/16/32-bar boundaries. We score how
              much the sound changes at each possible boundary ("novelty") and pick the
              set of boundaries with the best total score, preferring 8/16/32-bar
              phrases (dynamic programming, like the solver's shortest path).
  4. NAME     each part is labelled Intro / Build / Pre-drop / Fake drop / Drop /
              Breakdown / Outro from its loudness, bass and whether its highs rise
              (rules in section 4 below, worked out from hand-labelled tracks).

It depends on the beatgrid being right: if bar 1 / the downbeat is wrong in Rekordbox,
the boundaries will be off too.

All the tuning numbers are in SETTINGS below. The maths functions take plain numpy
arrays, so they can be tested with synthetic audio (tests/test_audio_analysis.py).
"""

from __future__ import annotations

import shutil
import subprocess
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .beatgrid import Beatgrid
from .models import Phrase

# Bump when the algorithm changes, so cached results are recomputed.
ANALYSIS_VERSION = 55

# A fingerprint of this file's code. It's part of the cache key too, so ANY edit to the
# detector (even one where we forgot to bump ANALYSIS_VERSION) makes the app re-analyse
# tracks with the new rules - just refresh the page.
CODE_HASH = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]

SETTINGS = {
    # Audio is analysed at this sample rate (Hz). 11025 keeps everything up to ~5.5 kHz,
    # plenty for telling drops from breakdowns, and is fast.
    "sample_rate": 11025,
    # FFT frame size and hop (samples) for the spectrogram.
    "frame": 2048,
    "hop": 512,
    # Frequency bands (Hz) used for naming sections.
    "bass_band": (25, 120),  # sub bass + kick
    "high_band": (2500, 5500),  # hats, top end
    # Bars either side of a boundary compared when measuring how much the sound changes.
    "novelty_window_bars": 4,
    # How much the sound changes is measured in dB (average across 12 frequency bands).
    # Real section changes on typical tracks: ~10-40 dB; normal bar-to-bar wobble: 1-3 dB.
    # Changes are capped at this (so silence at the very end doesn't drown everything
    # else out) and divided by `novelty_scale_db` to give the score used below.
    "novelty_cap_db": 15.0,
    "novelty_scale_db": 10.0,
    # Only the strongest bar within this many bars of a change counts in full; the
    # others keep `off_peak_factor` of their score.
    "peak_radius_bars": 2,
    "off_peak_factor": 0.2,
    # Phrase boundaries can only fall on this grid (bars).
    "grid_bars": 4,
    # Score lost for shifting that grid away from bar 1 (so one odd bar, like the bass
    # sneaking in a bar early, can't shift every boundary in the track). 2.0 since v55:
    # T15's fills on the last bar of each phrase pulled its grid a bar early at 1.0.
    "grid_offset_penalty": 2.0,
    # Score each boundary has to beat (higher = fewer, longer phrases). With a scale of
    # 10 dB, 0.25 means a change of about 2.5 dB can start a new phrase. (Some tracks'
    # sections differ by only 2-4 dB. Extra splits are harmless: neighbouring sections
    # that sound the same are merged again when naming them.)
    "boundary_cost": 0.25,
    # Bonus for phrases of these lengths (bars): dance music loves 8/16/32.
    "length_bonus": {8: 0.2, 16: 0.25, 32: 0.2, 4: -0.1},
    # Longest phrase allowed (bars) before it must be split.
    "max_phrase_bars": 64,
    # Two neighbouring sections whose sound differs by less than this (dB) are treated
    # as one (the same groove carrying on).
    "same_sound_db": 2.0,
    # A section counts as a DROP when its bass AND overall loudness are close to the
    # track's LOUDEST SECTION: within these many dB. (From labelled tracks: intros with
    # bass, builds and breakdowns can be just 2-4 dB quieter than the drop, so this has
    # to be strict. Some drops alternate loud/quiet bars, which is why we compare
    # sections with sections rather than with the single loudest bars.)
    "drop_bass_within_db": 4.0,
    "drop_energy_within_db": 1.5,
    # ...and its highs (top end) within this many dB of the brightest section's.
    "drop_highs_within_db": 5.0,
    # BUILD: the highs rise by at least this much (dB) across the section...
    "build_rise_db": 2.5,
    # ...or its bass falls by at least this much (dB) across a section of at most
    # positional_build_max_bars (thinning out).
    "build_bass_fall_db": 6.0,
    # ...or its overall loudness climbs by at least this much (dB) into the drop (some
    # builds keep the highs flat and just swell, e.g. T8 bars 33-40).
    "build_energy_rise_db": 3.0,
    # A section that sounds this close (dB) to an earlier fake drop is that part
    # coming back.
    "section_repeat_db": 1.5,
    # ...or it's a different-sounding section of at most this many bars right before a drop.
    "positional_build_max_bars": 8,
    # PRE-DROP: bars just before a drop where the bass is this much (dB) below the
    # build-up before it (measured over bars `predrop_ref_bars` = (from, to) bars back).
    "predrop_cut_db": 6.0,
    "predrop_ref_bars": (8, 3),
    "predrop_max_bars": 4,
    # Lengths a pre-drop can have (bars); any other length is redrawn (see the end of
    # classify_sections).
    "predrop_lengths": (1, 2, 4),
    # GROOVE vs BREAKDOWN (sections between drops that aren't builds): a groove keeps
    # its bass within this many dB of the drop's; a breakdown drops it further.
    "groove_bass_within_db": 8.0,
    # After the opening and before any build: a section whose bass is within this many
    # dB of the drop's is a GROOVE, not intro (intros with bass sit 3-5 dB below).
    "groove_full_bass_db": 2.0,
    # The bass counts as "in" during the intro when it's within this many dB of the
    # drop's (no bass at all measures 40-80 dB down; T6's on/off stutter bass ~10).
    "intro_bass_in_db": 15.0,
    # ...and a later intro section whose bass is no more than this many dB below where
    # the bass came in is the groove (T6 17-24: 0.7 dB below; T8 25-32, still
    # intro: 3.2 dB below).
    "groove_bass_keep_db": 1.5,
    # ...and its bass (upper quarter of its bars) is within this many dB of the drop's.
    "groove_bass_near_drop_db": 6.0,
    # ...unless its highs climb by at least this much across it: a filter opening up,
    # still the intro.
    "intro_filter_rise_db": 8.0,
    # A later "fake drop" that sounds this close (dB) to the opening groove is that groove
    # coming back (T4 77-84: 2.4 dB; its real fake drops: 5+ dB).
    "groove_return_db": 3.0,
    # The opening coming back after a breakdown, bar for bar = a GROOVE: at least this
    # many bars, each within groove_copy_bar_db and on average within groove_copy_mean_db
    # of the opening bars (real returns: 0-0.6 dB; looser copies that the user calls a
    # breakdown: ~3 dB).
    "groove_copy_min_bars": 8,
    "groove_copy_bar_db": 3.0,
    "groove_copy_mean_db": 2.0,
    # A full-level section that sounds this close (dB) to an earlier groove is that
    # groove coming back.
    "groove_repeat_db": 2.0,
    # ...and lasts at least this long (shorter ones are breakdowns). Provisional: needs
    # hand-labelled groove examples to tune.
    "groove_min_bars": 16,
    # FAKE DROP: between this many bars long, leading into another build-up.
    "fakedrop_min_bars": 3,
    "fakedrop_max_bars": 8,
    # REPEATED BUILD: a later stretch counts as a copy of an earlier build (+ pre-drop)
    # if its bars differ from the build's by less than this on average (dB). Exact
    # copies measure ~0-1 dB; copies with vocals on top ~2-3 dB; different parts 5+.
    "repeat_bar_db": 3.0,
    # ...for builds of at least this many bars.
    "repeat_min_bars": 4,
    # Sections shorter than this (bars) are merged into a neighbour (except pre-drops).
    "min_section_bars": 2,
    # BUILD at the end of a long intro/breakdown: the last `lead_in_bars` before a drop
    # count as a build if their bass is this much (dB) lower than the bars before them.
    "lead_in_bars": 8,
    "lead_in_bass_drop_db": 2.5,
    # SHORT BUILD: the last `short_build_bars` before a drop count as a build if their
    # highs are this much (dB) above the `short_build_ref_bars` bars before them.
    "short_build_bars": 2,
    "short_build_ref_bars": 4,
    "short_build_highs_db": 2.5,
    # 4-BAR BUILD: the last `lead_in4_bars` before a drop count as a build if they sound
    # at least `lead_in4_change_db` different from the `lead_in4_ref_bars` before them
    # AND their bass is at least `lead_in4_bass_db` lower (thinning out).
    "lead_in4_bars": 4,
    "lead_in4_ref_bars": 8,
    "lead_in4_change_db": 4.0,
    "lead_in4_bass_db": 2.0,
    # ...and they're no more than this much (dB) quieter overall (T9: +2 louder;
    # a 2-bar bass cut at the end of a groove, like T5 31-32: 5 dB quieter).
    "lead_in4_quieter_db": 1.0,
    # A "build" that's STRIPPED (bass at least this many dB under the drop's), flat, and
    # only swells in its last 2 bars (louder by swell_energy_db AND bass back up by
    # swell_bass_db) is a breakdown + a 2-bar build (T10: bass -47 -> -27,
    # +2 dB louder).
    "stripped_bass_db": 20.0,
    "swell_energy_db": 1.5,
    "swell_bass_db": 10.0,
    # ...or that's flat for its first half and then gets this much (dB) BRIGHTER (median
    # highs) for its second half is half breakdown, half build (T14 17-24: +5.4).
    "half_build_highs_jump_db": 5.0,
    # A build is never longer than this (bars).
    "build_max_bars": 16,
    # PRE-DROP cuts make the track quieter: a bar whose bass dips but that's more than
    # this much (dB) LOUDER overall than the build before it isn't a cut (T9 32).
    "predrop_max_louder_db": 1.5,
    # ARRIVAL: the bar where the bass slams back in after a pre-drop cut (start of a
    # drop or fake drop). The bass must jump up by at least this much (dB)...
    "arrival_jump_db": 12.0,
    # ...to within this much of the track's loudest bass.
    "arrival_bass_within_db": 6.0,
    # ...and stay in for the next this-many bars (not cut again by as much as it jumped).
    "arrival_stay_bars": 3,
    # A lead-in bar (bass on beats 1-3, gap on beat 4, drop next bar) moves any arrival
    # found up to this many bars before the drop onto the drop itself.
    "lead_in_lookback_bars": 3,
    # ...and its beats 1-3 have to be steady: within this many dB of each other
    # (T11's lead-ins: 1-2.5 dB; T3's bouncing drop rhythm: ~12).
    "lead_in_steady_db": 4.0,
    # Arrivals add this to the novelty score of their bar, so the 4/8-bar phrase grid
    # lines up with where the drops actually land (T11: phrases on bars 3,
    # 11, 19..., which one-bar cuts at 18 and 50 otherwise pull a bar early).
    "arrival_novelty_bonus": 0.5,
    # A section "stutters" when more than this share of its bars have the bass cut out
    # (by arrival_jump_db below its loudest bar). Drops with the odd switch-up cut stay
    # well under it (~0-12%); T11's groove/builds (bass hits with gaps) are 25-62%.
    "stutter_cut_fraction": 0.2,
    # A drop's start is pulled back over nearly-full bars before it, but not over the
    # top of a build's climb: a bar whose bass rose by at least this much (dB) on each
    # of the last two bars is still the build.
    "climb_step_db": 2.0,
    # Drop | "build" | much brighter build | drop: the first "build" is really the
    # breakdown if the highs jump up by at least this much (dB) into the second.
    "build_highs_jump_db": 6.0,
    # Quieter bits this short (bars) sitting between two drop sections count as part of
    # the drop (DnB drops often cut the bass for a few bars as a switch-up).
    "max_dip_in_drop_bars": 8,
    # A part right after a drop that copies the opening groove/intro bar for bar (within
    # this many dB on average) is a breakdown (T14 57-64: 0.1 dB).
    "opening_copy_db": 1.0,
    # A breakdown running into a part that sounds at least this different (dB) - 8+ bars,
    # before the build - makes that part a groove ("you can't break down a breakdown").
    "breakdown_then_groove_db": 5.0,
    # Bars at the very end this many dB below the track's loud parts are silence: outro.
    "silence_db": 20.0,
    # Next to a drop: a part whose sound differs from the typical drop bar by less than
    # this (dB) in EVERY frequency band (narrow margin: 6.3 still drop, 7.5 breakdown) is the drop with something dropped out; more in
    # any one band (a bassline gone, a new riff) and it's a different part.
    "drop_variation_db": 7.0,
}


# --------------------------------------------------------------------------------------
# 1. Decode
# --------------------------------------------------------------------------------------


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def decode_mono(path: Path, sample_rate: int = SETTINGS["sample_rate"]) -> np.ndarray:
    """
    Decode any audio file ffmpeg understands into mono float32 samples. READ-ONLY:
    output goes to a pipe, never to a file. Raises RuntimeError if it fails.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
           "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg couldn't decode the file: {result.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(result.stdout, dtype=np.float32)


# --------------------------------------------------------------------------------------
# 2. Measure each bar
# --------------------------------------------------------------------------------------


@dataclass
class BarFeatures:
    """Per-bar measurements (arrays indexed by bar: index 0 = bar 1)."""

    energy: np.ndarray  # overall loudness (dB)
    bass: np.ndarray  # sub/kick band (dB)
    highs: np.ndarray  # high band (dB)
    bands: np.ndarray  # shape (bars, 12): "fingerprint" in dB per band
    # Bass (dB) on each of the 4 beats of every bar, shape (bars, 4). Lets us see a
    # bass gap on just one beat (e.g. a lead-in bar before a drop). None in tests that
    # only give per-bar values.
    bass_beats: Optional[np.ndarray] = None


def _band_powers(samples: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Short-time power in the bands we care about, frame by frame.
    Returns (times[frames], powers[frames, 15]) where the 15 columns are:
      0 = total, 1 = bass band, 2 = high band, 3..14 = the 12 fingerprint bands.
    Done in chunks so a long track doesn't need hundreds of MB of memory.
    """
    frame, hop = SETTINGS["frame"], SETTINGS["hop"]
    samples = samples.astype(np.float32, copy=False)
    if len(samples) < frame:
        samples = np.pad(samples, (0, frame - len(samples)))
    n_frames = 1 + (len(samples) - frame) // hop
    freqs = np.fft.rfftfreq(frame, 1 / sr)
    window = np.hanning(frame).astype(np.float32)

    # Which FFT bins belong to each band
    edges = np.geomspace(40, sr / 2 * 0.98, 13)  # 12 log-spaced fingerprint bands
    selectors = [np.ones_like(freqs, dtype=bool),
                 (freqs >= SETTINGS["bass_band"][0]) & (freqs < SETTINGS["bass_band"][1]),
                 (freqs >= SETTINGS["high_band"][0]) & (freqs < SETTINGS["high_band"][1])]
    selectors += [(freqs >= edges[k]) & (freqs < edges[k + 1]) for k in range(12)]
    # One matrix multiply sums the bins of every band at once
    band_matrix = np.stack(selectors, axis=1).astype(np.float32)  # (bins, 15)

    out = np.empty((n_frames, band_matrix.shape[1]), dtype=np.float64)
    chunk = 1024
    for c0 in range(0, n_frames, chunk):
        c1 = min(n_frames, c0 + chunk)
        idx = np.arange(frame)[None, :] + hop * np.arange(c0, c1)[:, None]
        spec = np.abs(np.fft.rfft(samples[idx] * window, axis=1)) ** 2
        out[c0:c1] = spec @ band_matrix
    times = (np.arange(n_frames) * hop + frame / 2) / sr
    return times, out


def bar_features(samples: np.ndarray, sr: int, grid: Beatgrid) -> BarFeatures:
    """Measure every complete bar of the grid."""
    times, powers = _band_powers(samples, sr)
    n_bars = grid.bar_count

    def db(x):
        """power -> decibels"""
        return 10 * np.log10(x + 1e-10)

    # Only use analysis frames that sit ENTIRELY inside a bar. A frame overlapping the
    # next bar would let e.g. the first kick of a drop leak into the bar before it.
    half = SETTINGS["frame"] / 2 / sr
    per_bar = np.zeros((n_bars, powers.shape[1]))
    bass_beats = np.zeros((n_bars, 4))
    for b in range(n_bars):
        # bass per beat (frames whose centre falls in the beat; a beat is too short to
        # insist on whole frames)
        for k in range(4):
            q0, q1 = grid.bar_to_time(b + 1 + k / 4), grid.bar_to_time(b + 1 + (k + 1) / 4)
            in_beat = (times >= q0) & (times < q1)
            if in_beat.any():
                bass_beats[b, k] = db(powers[in_beat, 1].mean())
            else:
                bass_beats[b, k] = db(powers[min(np.searchsorted(times, q0), len(times) - 1), 1])
        t0, t1 = grid.bar_to_time(b + 1), grid.bar_to_time(b + 2)
        sel = (times - half >= t0) & (times + half <= t1)
        if not sel.any():  # bar shorter than a frame (shouldn't happen): nearest frame
            sel = np.zeros_like(times, dtype=bool)
            sel[min(np.searchsorted(times, t0), len(times) - 1)] = True
        per_bar[b] = db(powers[sel].mean(axis=0))

    return BarFeatures(energy=per_bar[:, 0], bass=per_bar[:, 1], highs=per_bar[:, 2], bands=per_bar[:, 3:],
                       bass_beats=bass_beats)


# --------------------------------------------------------------------------------------
# 3. Split into phrases
# --------------------------------------------------------------------------------------


def novelty(bands: np.ndarray) -> np.ndarray:
    """
    How much the sound changes at the START of each bar (index b = start of bar b+1):
    the average dB difference (RMS over the 12 bands) between the W bars before and
    the W bars after. Softly capped at novelty_cap_db, then divided by novelty_scale_db.
    These are absolute dB, not scaled per track, so a track with no big changes
    doesn't get its small wobbles blown up into "boundaries".
    """
    n = len(bands)
    w = SETTINGS["novelty_window_bars"]
    nov = np.zeros(n)
    for b in range(1, n):
        before = bands[max(0, b - w):b].mean(axis=0)
        after = bands[b:min(n, b + w)].mean(axis=0)
        nov[b] = np.sqrt(np.mean((after - before) ** 2))
    # Soft cap (tanh): huge changes are squashed towards the cap, but stay in order,
    # so the exact bar where the change happens still scores highest.
    # A change also shows up (more weakly) in the bars around it, because the windows
    # overlap. Keep full score only for the PEAK bar within +-peak_radius bars; its
    # neighbours keep a fraction, so two near-misses can't outscore one exact hit.
    # (Peaks are found on the raw dB values, BEFORE the cap below flattens them.)
    r = SETTINGS["peak_radius_bars"]
    weight = np.ones(n)
    for b in range(n):
        if nov[b] < nov[max(0, b - r):b + r + 1].max():
            weight[b] = SETTINGS["off_peak_factor"]

    cap = SETTINGS["novelty_cap_db"]
    return weight * cap * np.tanh(nov / cap) / SETTINGS["novelty_scale_db"]


def choose_boundaries(nov: np.ndarray) -> list[int]:
    """
    Pick phrase start bars (1-based, always including bar 1) by dynamic programming.

    Boundaries may fall on a grid of `grid_bars` (4) starting from bar 1 + offset; we try
    every offset (0-3 bars) and keep the best, in case the phrasing doesn't start on
    bar 1. Score = sum of novelty at chosen boundaries - boundary_cost per boundary
    + length bonuses. Returns e.g. [1, 17, 25, 57, ...].
    """
    n = len(nov)
    if n == 0:
        return []
    grid = SETTINGS["grid_bars"]
    bonus = SETTINGS["length_bonus"]
    max_len = SETTINGS["max_phrase_bars"]
    cost = SETTINGS["boundary_cost"]
    S_offset_penalty = SETTINGS["grid_offset_penalty"]

    best_score, best_bounds = -np.inf, [1]
    for offset in range(grid):
        # candidate phrase starts (0-based bar index) + the end of the track
        points = [0] + [p for p in range(offset, n, grid) if p > 0] + [n]
        points = sorted(set(points))
        score = [-np.inf] * len(points)
        prev = [-1] * len(points)
        score[0] = 0.0
        for j in range(1, len(points)):
            for i in range(j - 1, -1, -1):
                length = points[j] - points[i]
                if length > max_len:
                    break
                # the first phrase may be short (offset pickup); others must be >= grid
                if length < grid and i != 0:
                    continue
                gain = 0.0 if j == len(points) - 1 else nov[points[j]] - cost
                s = score[i] + gain + bonus.get(length, 0.0)
                if s > score[j]:
                    score[j], prev[j] = s, i
        # walk back from the end
        bounds, j = [], len(points) - 1
        while j > 0:
            j = prev[j]
            bounds.append(points[j])
        bounds = sorted(bounds)
        # Phrasing normally counts from bar 1; only shift the grid when the music
        # clearly says so (e.g. a silent first bar), not because of one odd bar.
        total = score[-1] - (S_offset_penalty if offset else 0.0)
        if total > best_score:
            best_score, best_bounds = total, [b + 1 for b in bounds]
    return best_bounds


# --------------------------------------------------------------------------------------
# 4. Name the phrases
# --------------------------------------------------------------------------------------
#
# Rules, worked out from hand-labelled tracks (phrase_labels.json):
#   DROP      full bass AND loudness: within a dB or two of the track's loudest parts.
#             (Not "loud-ish": intros with bass, builds and breakdowns can be only
#             2-4 dB quieter than the drop.)
#   BUILD     the highs RISE across the section (risers, rolls, filter sweeps), or a
#             short section that's different from what came before, right before a drop.
#   PRE-DROP  1-4 bars just before a drop (or fake drop) where the bass is pulled out.
#             Found bar-by-bar, so it doesn't have to fit the 4/8-bar phrase grid.
#   FAKE DROP comes where a drop is expected (straight after a build/pre-drop) but is
#             short and/or not full, and goes into ANOTHER build before the real drop.
#             Tracks often copy the first drop's build-up exactly, so you can't tell a
#             fake drop from its build-up: only from what happens after it.
#   GROOVE    between drops, beat and bass still going but below drop level (filler).
#   INTRO / BREAKDOWN / OUTRO  anything else: before anything happens / between drops
#             with the bass mostly gone / after the last drop.

KIND_NAMES = {
    "intro": "Intro", "build": "Build", "predrop": "Pre-drop", "fakedrop": "Fake drop",
    "drop": "Drop", "groove": "Groove", "breakdown": "Breakdown", "outro": "Outro",
}


@dataclass
class Section:
    start: int  # first bar (1-based)
    end: int  # bar just after the section
    kind: str = ""

    @property
    def length(self) -> int:
        return self.end - self.start


def _relative(feats: BarFeatures) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bar levels relative to the track's loud parts (its 90th-percentile bar), in dB."""
    return tuple(x - np.percentile(x, 90) for x in (feats.energy, feats.bass, feats.highs))


def _rise(values: np.ndarray) -> float:
    """How much a series climbs from its first to its last bar (dB, straight-line fit)."""
    if len(values) < 2:
        return 0.0
    slope = np.polyfit(np.arange(len(values)), values, 1)[0]
    return float(slope * (len(values) - 1))


def _sound_distance(feats: BarFeatures, a: Section, b: Section) -> float:
    """Average dB difference between two sections' fingerprints."""
    fa = feats.bands[a.start - 1:a.end - 1].mean(axis=0)
    fb = feats.bands[b.start - 1:b.end - 1].mean(axis=0)
    return float(np.sqrt(np.mean((fa - fb) ** 2)))


def find_arrivals(feats: BarFeatures) -> list[int]:
    """
    Bars where the bass SLAMS back in after being cut: the first bar of a drop or a
    fake drop (in every labelled example so far, these are preceded by 1-4 bars with
    the bass pulled out). Returns 1-based bar numbers.
    """
    S = SETTINGS
    _, B, _ = _relative(feats)
    out = []
    stayed = set()  # candidates where the bass stays in afterwards
    for b in range(1, len(B)):  # b = 0-based index of the candidate arrival bar
        jump = B[b] - B[b - 1]
        if jump >= S["arrival_jump_db"] and B[b] >= -S["arrival_bass_within_db"]:
            out.append(b + 1)
            # It also has to STAY in: if it's cut again (by as much as it jumped) within
            # the next `arrival_stay_bars` bars, it was just a hit in a stuttering groove
            # or build, not a drop arriving (T11 19-34: one bar of bass,
            # then gaps). Real drops and fake drops keep it within ~7 dB.
            after = B[b + 1:b + 1 + S["arrival_stay_bars"]]
            if not len(after) or after.min() > B[b] - S["arrival_jump_db"]:
                stayed.add(b + 1)
    # A "slam-in" every 2 bars in a row is a stutter rhythm (bass on/off), not a drop
    # arriving: drop those. (Checked against ALL candidates, including ones that didn't
    # stay in, so a stutter is still recognised as one.)
    found = set(out)
    arrivals = [a for a in out if a in stayed and a - 2 not in found and a + 2 not in found]

    # LEAD-IN BAR before a drop: after a bass cut, the bass comes back for beats 1-3
    # of a bar, drops out on beat 4, and the drop hits on the NEXT bar. That bar (and
    # any bass hits just before it) is still the build-up; the drop starts after the
    # gap. E.g. T11: 33 cut | 34 bass + gap on beat 4 | 35 drop, and
    # 56 cut | 57 hit | 58 bass + gap | 59 drop. (A gap on another beat is just the
    # drop's rhythm: T5 bar 33 has one on beat 2 and is the drop. And beats 1-3 must
    # be STEADY: T3's drop bounces 0 / -12 / -4 / -21 dB every other bar, which
    # isn't a lead-in.)
    if feats.bass_beats is not None:
        beats = feats.bass_beats - np.percentile(feats.bass, 90)  # same scale as B
        jump, within = S["arrival_jump_db"], S["arrival_bass_within_db"]
        for i in range(1, len(B) - 1):  # i = 0-based index of the lead-in bar
            first3 = float(np.median(beats[i, :3]))
            after = B[i + 2:i + 2 + S["arrival_stay_bars"]]
            steady = np.ptp(beats[i, :3]) <= S["lead_in_steady_db"]
            if (first3 >= -within and steady and beats[i, 3] <= first3 - jump  # bass in, gap on 4
                    and B[i + 1] >= -within                                 # drop next bar...
                    and (not len(after) or after.min() > B[i + 1] - jump)  # ...staying in
                    and B[max(0, i - 3):i].min() <= B[i + 1] - jump):       # after a cut
                drop = i + 2  # 1-based bar of the drop
                arrivals = [a for a in arrivals if not drop - S["lead_in_lookback_bars"] <= a < drop]
                if drop not in arrivals:
                    arrivals.append(drop)
        arrivals.sort()
    return arrivals


def classify_sections(starts: list[int], n_bars: int, feats: BarFeatures) -> list[Section]:
    """Turn phrase start bars into labelled Sections (may add pre-drop sections)."""
    S = SETTINGS
    E, B, H = _relative(feats)
    # Arrivals (bass slamming back in) are always section starts, on or off the grid.
    starts = sorted(set(starts) | set(find_arrivals(feats)))
    starts = [b for i, b in enumerate(starts) if i == 0 or b - starts[i - 1] >= 1]
    secs = [Section(s, e) for s, e in zip(starts, starts[1:] + [n_bars + 1])]
    arrival_set = set(find_arrivals(feats))

    # (a) Neighbours that sound the same are one section (the same groove carrying on).
    merged = [secs[0]]
    for sec in secs[1:]:
        if sec.start not in arrival_set and _sound_distance(feats, merged[-1], sec) < S["same_sound_db"]:
            merged[-1].end = sec.end
        else:
            merged.append(sec)
    secs = merged
    n = len(secs)

    def bars(sec: Section, arr: np.ndarray) -> np.ndarray:
        return arr[sec.start - 1:sec.end - 1]

    # (b) Which sections are "full" (drop level)? Compared with the loudest section
    #     of at least 4 bars.
    #     A section's level is its MEDIAN bar, so a couple of cut bars at its end
    #     (e.g. a 2-bar bass cut before the next part) don't make it look quiet.
    def level(sec: Section, arr: np.ndarray) -> float:
        return float(np.median(bars(sec, arr)))

    #     A drop needs its HIGHS up as well as loudness and bass: a fake drop or a
    #     breakdown can have the loudest bass in the track but the top end pulled out.
    #     The reference ("top") is the loudest section among those with their highs up.
    #     A STUTTERING section (bass hits with gaps: many bars with the bass cut right
    #     out) can't be the reference: its hits are often louder than the steady drop
    #     itself (T11: groove/build hits +0.5 dB, drop -2 dB).
    def stutters(sec: Section) -> bool:
        b = bars(sec, B)
        cut = b <= b.max() - S["arrival_jump_db"]
        return cut.mean() > S["stutter_cut_fraction"]

    long_secs = ([s for s in secs if s.length >= 4 and not stutters(s)]
                 or [s for s in secs if s.length >= 4] or secs)
    top_h = max(level(s, H) for s in long_secs)
    bright = [s for s in long_secs if level(s, H) >= top_h - S["drop_highs_within_db"]] or long_secs
    top_e = max(level(s, E) for s in bright)
    top_b = max(level(s, B) for s in bright)
    def is_full(sec: Section) -> bool:
        return (level(sec, E) >= top_e - S["drop_energy_within_db"]
                and level(sec, B) >= top_b - S["drop_bass_within_db"]
                and level(sec, H) >= top_h - S["drop_highs_within_db"])

    full = [is_full(s) for s in secs]
    for i in range(1, n - 1):  # short dips inside a drop are part of it...
        # ...unless it's a cut before an arrival.
        if (not full[i] and full[i - 1] and full[i + 1] and secs[i].length <= S["max_dip_in_drop_bars"]
                and secs[i + 1].start not in arrival_set):
            full[i] = True

    # DROPS KEEP CHANGING: parts (vocals, a lead) drop out for a few bars to keep a long
    # drop interesting - that's still the drop. But if one part of the sound changes a
    # LOT (a bassline gone, a new riff or the intro's alarm back), it's a breakdown.
    # Measured per frequency band against the typical drop bar: the biggest change
    # decides. (Still drop: T10 42-48, 57-64, 81-88 at 3.7-4.7 dB.
    # Breakdown: T7 66-73 at 7.5, T11 75-82 at 9.3. Still drop,
    # but close: T9 89-96 and 185-192 at 6.3.)
    full_rows = [b for k in range(n) if full[k] for b in range(secs[k].start - 1, secs[k].end - 1)]
    different_part: set[int] = set()  # start bars of parts that sound unlike the drop
    if full_rows:
        drop_fp = np.median(feats.bands[full_rows], axis=0)

        def drop_change(sec: Section) -> float:
            """Biggest change (dB) in any one band, typical bar vs typical drop bar."""
            return float(np.max(np.abs(np.median(bars(sec, feats.bands), axis=0) - drop_fp)))

        before = list(full)
        for i in range(n):
            sec = secs[i]
            # (looking past a 1-2 bar cut: drops often cut the bass for a bar as a
            # switch-up, e.g. T10 41, then carry on)
            prev = i - 1
            if prev > 0 and not before[prev] and secs[prev].length <= 2:
                prev -= 1
            next_to_drop = (prev >= 0 and before[prev]) or (i + 1 < n and before[i + 1])
            # (only once the first drop has started, and only short parts: an intro with
            # the bass nearly in, or a long outro, isn't the drop)
            after_first_drop = any(before[:i])
            if (not next_to_drop or not after_first_drop or sec.length < 4
                    or sec.length > S["max_dip_in_drop_bars"]):
                continue
            change = drop_change(sec)
            if not before[i] and change < S["drop_variation_db"] and not stutters(sec):
                full[i] = True  # the drop with a part dropped out
            elif (before[i] and change >= S["drop_variation_db"] and sec.length <= S["max_dip_in_drop_bars"]
                  and prev >= 0 and before[prev]):
                full[i] = False  # a different part inside the drop: breakdown
                different_part.add(sec.start)
    rises = [_rise(bars(s, H)) for s in secs]
    # A section with clearly rising highs is a build-up, even if it's nearly as loud as
    # the drop (some builds keep the bass in) - as long as a drop is still to come.
    for i in range(n):
        # (not a long stretch: a 50-bar "build" is a drop that starts with a couple of
        # quiet bars, which makes its highs look like they rise - T9 bar 129)
        if (full[i] and rises[i] >= S["build_rise_db"] and i > 0 and not full[i - 1]
                and secs[i].length <= S["build_max_bars"]
                and any(full[j] for j in range(i + 1, n))):
            full[i] = False

    # GROOVE: a full-level stretch that ISN'T a drop. A drop is where the bass slams
    # back in after a cut or build (an "arrival"); a groove just carries on - after the
    # intro, or after a short break - and then leads (maybe via a short cut) into a
    # real slam-in drop. Grooves can come back later in the track.
    def cut_just_before(bar: int) -> bool:
        """Is the bar before `bar` a bass cut compared with the few bars before it?"""
        if bar < 4:
            return False
        ref = float(np.median(B[max(0, bar - 9):bar - 3]))
        return B[bar - 2] <= ref - S["predrop_cut_db"]

    groove = [False] * n
    for i in range(n):
        # Does section i "carry on" (full level, but no slam-in: it follows the intro /
        # a quiet part, or comes back after a short cut)?
        if not full[i] or groove[i] or secs[i].start in arrival_set:
            continue
        if not (i == 0 or not full[i - 1] or cut_just_before(secs[i].start)):
            continue
        j = i  # extend over following full sections, up to the next slam-in / cut
        while (j + 1 < n and full[j + 1] and secs[j + 1].start not in arrival_set
               and not cut_just_before(secs[j + 1].start)):
            j += 1
        nxt = j + 1
        if nxt < n and not full[nxt] and secs[nxt].length <= S["predrop_max_bars"]:
            nxt += 1  # look past a short cut
        if nxt < n and full[nxt] and secs[nxt].start in arrival_set:
            for k in range(i, j + 1):
                groove[k], full[k] = True, False

    # A groove that COMES BACK later is a groove too, even if the bass happens to come
    # back in hard enough to look like a slam-in: it sounds like the earlier groove.
    found = [k for k in range(n) if groove[k]]
    for i in range(n):
        if not full[i] or groove[i] or not found:
            continue
        if min(_sound_distance(feats, secs[i], secs[g]) for g in found) < S["groove_repeat_db"]:
            groove[i], full[i] = True, False
    later_full = [any(full[j] for j in range(i + 1, n)) for i in range(n)]

    def leads_into_drop(i: int) -> bool:
        """Does section i lead (maybe via a short cut) into a drop / bass slam-in?"""
        j = i + 1
        if j < n and secs[j].start in arrival_set:
            return True  # the bass slams in straight after it
        if j < n and not full[j] and not groove[j] and secs[j].length <= S["predrop_max_bars"]:
            j += 1
        return j < n and not groove[j] and (full[j] or secs[j].start in arrival_set)

    # How much the bass falls across each section (dB, negative = thinning out), and how
    # much the overall loudness climbs (a swell into the drop).
    bass_trend = [_rise(bars(s, B)) for s in secs]
    energy_trend = [_rise(bars(s, E)) for s in secs]
    # Where the track really starts: a 1-bar scrap at the very start (e.g. a silent bar
    # before the beat kicks in) doesn't count as the opening section.
    first_real = 1 if n > 1 and secs[0].length < S["min_section_bars"] else 0

    kinds = ["drop" if f else "groove" if g else "" for f, g in zip(full, groove)]

    # (c) Builds: rising highs with a drop still to come, or a short section that's
    #     different from what came before, right before a drop.
    for i in range(n):
        if full[i] or groove[i]:
            continue
        # (a build has to lead into a drop: rising highs going into a groove are intro)
        # A build either has rising highs, or its bass thins out (pulled out bit by bit
        # to build tension) - either way it must lead into a drop.
        thinning = (bass_trend[i] <= -S["build_bass_fall_db"]
                    and secs[i].length <= S["positional_build_max_bars"])
        swelling = energy_trend[i] >= S["build_energy_rise_db"]
        rising = (rises[i] >= S["build_rise_db"] or thinning or swelling) and leads_into_drop(i)
        short_lead_in = (i + 1 < n and full[i + 1] and i > first_real and not full[i - 1]
                         and secs[i].length <= S["positional_build_max_bars"])
        # The track's opening section is an intro even if its highs rise (filter
        # opening up), unless it's a short lead straight into a drop. (If the beat slams
        # in right at the start, after a scrap bar, it's an intro whatever follows.)
        beat_from_start = first_real == 1 and secs[1].start in arrival_set
        opening = i <= first_real and (beat_from_start or not (
            i + 1 < n and full[i + 1] and secs[i].length <= S["positional_build_max_bars"]))
        if (rising or short_lead_in) and not opening:
            kinds[i] = "build"

    # (d) Fake drops: where a drop was expected (the bass slams back in after a build)
    #     but it isn't the full drop, and a build / the real drop follows.
    for i in range(n):
        before = kinds[i - 1] if i > 0 else ""
        if before == "" and i > 1 and secs[i - 1].length <= S["predrop_max_bars"]:
            before = kinds[i - 2]  # look past a short pre-drop cut
        if secs[i].start in arrival_set and not full[i] and later_full[i] and before == "build":
            kinds[i] = "fakedrop"
        elif (0 < i < n - 1 and kinds[i - 1] == "build" and kinds[i + 1] == "build"
              and secs[i].length <= S["fakedrop_max_bars"] and kinds[i] in ("", "drop")):
            kinds[i] = "fakedrop"

    # (d2) Repeats: a section that sounds like an earlier FAKE DROP is that part coming
    #      back. After a build it's another fake drop; after a drop (nothing building
    #      into it) it's a breakdown.
    for i in range(n):
        if kinds[i] not in ("", "drop") or secs[i].start in arrival_set:
            continue
        for j in range(i):
            if kinds[j] == "fakedrop" and _sound_distance(feats, secs[i], secs[j]) < S["section_repeat_db"]:
                kinds[i] = "fakedrop" if i > 0 and kinds[i - 1] == "build" else "breakdown"
                full[i] = False
                break

    # (e) Everything else: intro before anything has happened (no build/drop yet),
    #     outro after the last drop. In between: GROOVE if the beat and bass carry on
    #     (just not at full drop level), BREAKDOWN if the bass is mostly gone.
    events = [i for i, k in enumerate(kinds) if k in ("build", "fakedrop", "drop", "groove")]
    first_event = events[0] if events else n
    # (an outro is only an outro if nothing happens after it)
    last_event = events[-1] if events else -1
    for i in range(n):
        if not kinds[i]:
            if i < first_event:
                kinds[i] = "intro"
            elif i > last_event:
                kinds[i] = "outro"
            elif (bars(secs[i], B).mean() >= top_b - S["groove_bass_within_db"]
                  and secs[i].length >= S["groove_min_bars"]):
                kinds[i] = "groove"
            else:
                kinds[i] = "breakdown"
    for s, k in zip(secs, kinds):
        s.kind = k

    # A long "fake drop" usually builds up again in its second half: keep the first
    # part as the fake drop and call the rest a build if its highs rise.
    out: list[Section] = []
    for sec in secs:
        if sec.kind == "fakedrop" and sec.length > S["fakedrop_max_bars"] // 2:
            mid = sec.start + S["fakedrop_max_bars"] // 2
            rest = Section(mid, sec.end)
            if _rise(bars(rest, H)) >= S["build_rise_db"]:
                out += [Section(sec.start, mid, "fakedrop"), Section(mid, sec.end, "build")]
                continue
        out.append(sec)
    secs = out

    # A real fake drop leads into ANOTHER build-up, and lasts a few bars. Anything else
    # we called a fake drop is just part of the track's normal flow: a drop if it's
    # at full level, otherwise a breakdown (or intro if nothing has happened yet).
    for i, sec in enumerate(secs):
        if sec.kind != "fakedrop":
            continue
        nxt = secs[i + 1] if i + 1 < len(secs) else None
        if nxt is not None and nxt.kind != "build" and nxt.length <= S["predrop_max_bars"] and i + 2 < len(secs):
            nxt = secs[i + 2]  # look past a short cut
        if nxt is None or nxt.kind != "build" or sec.length < S["fakedrop_min_bars"]:
            level_ok = bars(sec, E).mean() >= top_e - S["drop_energy_within_db"]
            earlier = {s.kind for s in secs[:i]}
            sec.kind = "drop" if level_ok else ("breakdown" if earlier & {"build", "drop"} else "intro")

    # A drop often really starts a bar or two before the phrase grid says (e.g. after a
    # pre-drop of odd length): pull its start back over full-level bars.
    for i in range(1, len(secs)):
        sec, prev = secs[i], secs[i - 1]
        if sec.kind != "drop" or prev.kind == "drop":
            continue
        start = sec.start

        def still_climbing(bar: int) -> bool:
            """Is `bar` the top of a build's climb (bass rising bar after bar into it)?
            Then it belongs to the build, even if it's nearly at full level (e.g.
            T1 bar 53: bass -11, -7, -2 dB, then the drop)."""
            if bar < 3:
                return False
            rise = S["climb_step_db"]
            return B[bar - 1] - B[bar - 2] >= rise and B[bar - 2] - B[bar - 3] >= rise

        while start - 1 > prev.start and start > sec.start - 3 \
                and E[start - 2] >= top_e - S["drop_energy_within_db"] / 2 \
                and B[start - 2] >= top_b - S["drop_bass_within_db"] / 2 \
                and not still_climbing(start - 1):
            start -= 1
        prev.end = sec.start = start

    # A build hiding at the end of a long intro/breakdown: if the last
    # `lead_in_bars` before a drop have clearly less bass than the bars before them
    # (the bass being thinned out to build tension), split them off as a build.
    L = S["lead_in_bars"]
    out = []
    for i, sec in enumerate(secs):
        nxt = secs[i + 1] if i + 1 < len(secs) else None
        if (nxt is not None and nxt.kind == "drop" and sec.kind in ("intro", "breakdown", "groove")
                and sec.length >= 2 * L):
            # (typical bar, not average: a 2-bar cut right at the end mustn't count)
            head = float(np.median(B[sec.end - 1 - 2 * L:sec.end - 1 - L]))
            tail = float(np.median(B[sec.end - 1 - L:sec.end - 1]))
            if head - tail >= S["lead_in_bass_drop_db"]:
                rest_kind = sec.kind
                if rest_kind == "groove" and sec.length - L < S["groove_min_bars"]:
                    rest_kind = "breakdown"  # what's left is too short to be a groove
                out += [Section(sec.start, sec.end - L, rest_kind), Section(sec.end - L, sec.end, "build")]
                continue
        out.append(sec)
    secs = out

    # A SHORT build (e.g. 2 bars) at the very end of an intro/groove/breakdown, right
    # before a drop: the highs suddenly open up compared with the bars before (a drop
    # (almost) always has a build before it, even a short one). E.g. T1
    # bars 24-25.
    # A 4-bar build is checked first: the last 4 bars before the drop sound clearly
    # different from the 8 before them and the bass thins out (T9 29-32:
    # 4.4 dB different, bass 3 dB down; an intro running straight on measures < 3).
    K, ref = S["short_build_bars"], S["short_build_ref_bars"]
    L4, ref4 = S["lead_in4_bars"], S["lead_in4_ref_bars"]
    out = []
    for i, sec in enumerate(secs):
        nxt = secs[i + 1] if i + 1 < len(secs) else None
        if (nxt is not None and nxt.kind == "drop" and sec.kind in ("intro", "groove", "breakdown")
                and sec.length >= L4 + ref4):
            tail4 = slice(sec.end - 1 - L4, sec.end - 1)
            head4 = slice(sec.end - 1 - L4 - ref4, sec.end - 1 - L4)
            change = float(np.sqrt(np.mean((feats.bands[tail4].mean(axis=0) - feats.bands[head4].mean(axis=0)) ** 2)))
            thinning = float(np.median(B[head4]) - np.median(B[tail4]))
            # (and it mustn't get quieter overall: that's a bass CUT before the drop, which
            # the pre-drop / cut rules handle - e.g. T5 bars 31-32)
            louder = float(np.median(E[tail4]) - np.median(E[head4]))
            if (change >= S["lead_in4_change_db"] and thinning >= S["lead_in4_bass_db"]
                    and louder >= -S["lead_in4_quieter_db"]):
                out += [Section(sec.start, sec.end - L4, sec.kind), Section(sec.end - L4, sec.end, "build")]
                continue
        if (nxt is not None and nxt.kind == "drop" and sec.kind in ("intro", "groove", "breakdown")
                and sec.length >= K + ref):
            tail = float(np.median(H[sec.end - 1 - K:sec.end - 1]))
            head = float(np.median(H[sec.end - 1 - K - ref:sec.end - 1 - K]))
            if tail - head >= S["short_build_highs_db"]:
                out += [Section(sec.start, sec.end - K, sec.kind), Section(sec.end - K, sec.end, "build")]
                continue
        out.append(sec)
    secs = out

    secs = _split_off_predrops(secs, B, arrival_set, E)
    # With the cut bars split off, a "breakdown" that's actually at full level is part
    # of a drop (e.g. a drop that ended in a 2-bar cut).
    for sec in secs:
        if sec.kind == "breakdown" and sec.length >= 2 and is_full(sec) and sec.start not in different_part:
            sec.kind = "drop"
    secs = _keep_predrops_after_builds(secs)

    # A drop never goes straight into a groove (the user's rule): what comes after a
    # drop is a breakdown. That includes a quieter "groove" that only follows the drop
    # via a short cut (drop | 4-bar cut | quiet bit = one long breakdown). A full-level
    # groove after a short break is kept (e.g. T5 bars 65-78).
    for i in range(1, len(secs)):
        if secs[i].kind != "groove":
            continue
        prev = secs[i - 1]
        after_cut = (prev.kind == "breakdown" and prev.length <= S["predrop_max_bars"]
                     and i > 1 and secs[i - 2].kind == "drop")
        if prev.kind == "drop" or (after_cut and not is_full(secs[i])):
            secs[i].kind = "breakdown"

    # GROOVE after the opening, before anything has built up. The section where the
    # bass first comes in is still the intro; the intro is over (it's a groove) when
    # either
    #   - the bass is in at FULL drop level (e.g. T4 9-16, T5 17-30), or
    #   - a LATER section keeps the bass at least as strong as where it came in (T6
    #     17-24, T2 17-24). If the bass drops back again, it's still intro
    #     (T8 25-32).
    # Everything from there up to the first build is the groove carrying on.
    first_real = 1 if len(secs) > 1 and secs[0].length < S["min_section_bars"] else 0
    bass_came_in = None  # the intro section where the bass first came in
    for i in range(len(secs)):
        if secs[i].kind != "intro":
            break
        if i < first_real:
            continue
        bass = level(secs[i], B)
        # (bass hits with gaps: the hits are the bass level, not the typical bar, which
        # falls in a gap - T11 19-26. Only once the bass has already come
        # in: where it FIRST comes in is still the intro, hits or not - T6 9-16.)
        if bass_came_in is not None and secs[i].length >= 4 and stutters(secs[i]):
            bass = float(bars(secs[i], B).max())
        full_bass = i > first_real and bass >= top_b - S["groove_full_bass_db"]
        # (and the bass has to be near drop level - its upper quarter, so hits count:
        # T12 9-16 carries on from the intro but sits ~10 dB under the drop, still
        # intro; every groove so far is within ~5 dB)
        near_drop = float(np.percentile(bars(secs[i], B), 75)) >= top_b - S["groove_bass_near_drop_db"]
        carries_on = (bass_came_in is not None and near_drop
                      and bass >= level(bass_came_in, B) - S["groove_bass_keep_db"])
        # (Still the intro while a low-pass filter is opening up on it: the highs climb
        # right across the section. T11 11-18: highs +17 dB, bass in.)
        filter_opening = _rise(bars(secs[i], H)) >= S["intro_filter_rise_db"]
        if secs[i].length >= 4 and (full_bass or carries_on) and not filter_opening:
            j = i
            while j < len(secs) and secs[j].kind == "intro":
                secs[j].kind = "groove"
                j += 1
            break
        if bass_came_in is None and bass >= top_b - S["intro_bass_in_db"]:
            bass_came_in = secs[i]

    # The opening GROOVE COMING BACK after a drop: a later "fake drop" that sounds like
    # that groove is the groove again (e.g. T4 bars 77-84 = bars 9-16). Then
    # what we took for a build leading into it can't be one (it doesn't lead into a
    # drop) - straight after a drop it's a breakdown.
    # (Before the first drop, a fake drop stays a fake drop, even if it's groove-ish.)
    grooves = [s for s in secs if s.kind == "groove"]
    for i, sec in enumerate(secs):
        if (sec.kind != "fakedrop" or not grooves or sec.length < 4
                or not any(s.kind == "drop" for s in secs[:i])):
            continue
        if min(_sound_distance(feats, sec, g) for g in grooves) < S["groove_return_db"]:
            sec.kind = "groove"
            k = i - 1
            while k >= 0 and secs[k].kind in ("build", "predrop"):
                secs[k].kind = "breakdown"
                k -= 1

    # Straight after a drop it breaks down first. A "build" right after a drop that's
    # followed by a much brighter build (the highs jump up by build_highs_jump_db) was
    # really the breakdown, and the build starts at the jump. E.g. T1:
    # drop | 42-49 quiet, highs creeping up a bit | 50-53 highs up ~9 dB | drop.
    for i in range(1, len(secs) - 1):
        if (secs[i - 1].kind == "drop" and secs[i].kind == "build" and secs[i + 1].kind == "build"
                and level(secs[i + 1], H) - level(secs[i], H) >= S["build_highs_jump_db"]):
            secs[i].kind = "breakdown"

    # A "build" that's flat and stripped back (bass out, nothing rising) for most of it,
    # and only swells in its last couple of bars (louder, bass coming back), is really a
    # BREAKDOWN followed by a build: half and half (the user, 2026-10-14). E.g. T10
    # Yak Dub 17-24: bass gone and flat for 17-22, 23-24 swell into the drop; the user
    # hears breakdown 17-20, build 21-24 (the high-passed bass fading in from 21).
    K = S["short_build_bars"]
    out = []
    for i, sec in enumerate(secs):
        nxt = secs[i + 1] if i + 1 < len(secs) else None
        if sec.kind == "build" and sec.length >= K + 4 and nxt is not None \
                and nxt.kind in ("drop", "predrop", "fakedrop"):
            head = slice(sec.start - 1, sec.end - 1 - K)
            tail = slice(sec.end - 1 - K, sec.end - 1)
            stripped = float(np.median(B[head])) <= top_b - S["stripped_bass_db"]
            flat = (abs(_rise(E[head])) < S["build_energy_rise_db"] and _rise(H[head]) < S["build_rise_db"])
            swells = (float(np.median(E[tail]) - np.median(E[head])) >= S["swell_energy_db"]
                      and float(np.median(B[tail]) - np.median(B[head])) >= S["swell_bass_db"])
            if stripped and flat and swells:
                mid = sec.start + sec.length // 2
                out += [Section(sec.start, mid, "breakdown"), Section(mid, sec.end, "build")]
                continue
        # Same idea, but the second half gets BRIGHTER instead of swelling at the end:
        # a stripped 8+ bar "build" that's flat for its first half, then the highs step
        # up (risers come in) for the second half. Half breakdown, half build (the user,
        # 2026-10-15). E.g. T14 17-24: bass gone throughout, highs ~-6 dB for
        # 17-20, ~-1 dB from 21 (+5.4 dB).
        if sec.kind == "build" and sec.length >= 8 and sec.length % 2 == 0 and nxt is not None \
                and nxt.kind in ("drop", "predrop", "fakedrop"):
            mid = sec.start + sec.length // 2
            first = slice(sec.start - 1, mid - 1)
            second = slice(mid - 1, sec.end - 1)
            stripped = float(np.median(B[first])) <= top_b - S["stripped_bass_db"]
            flat = abs(_rise(H[first])) < S["build_rise_db"]
            brighter = float(np.median(H[second]) - np.median(H[first])) >= S["half_build_highs_jump_db"]
            if stripped and flat and brighter:
                out += [Section(sec.start, mid, "breakdown"), Section(mid, sec.end, "build")]
                continue
        out.append(sec)
    secs = out

    # A drop (almost) never comes without a build-up before it, even a short one (the
    # user's rule). Full-level bars straight after the intro, with no build, pre-drop or
    # bass slam-in leading into them, are a GROOVE. (E.g. T7 bars 10-17.)
    # A quieter bit that carries on from that groove into the first build is still the
    # groove (no drop has happened yet, so it can't be a breakdown).
    # (Short 2-bar builds before a drop were split off above, so a drop that has one
    # isn't caught here, e.g. T1 bars 24-25.)
    for i in range(1, len(secs)):
        if not (secs[i].kind == "drop" and secs[i - 1].kind == "intro"
                and secs[i].start not in arrival_set):
            continue
        r = i  # the whole full stretch: i..r
        while r + 1 < len(secs) and secs[r + 1].kind == "drop":
            r += 1
        j = r + 1  # ...plus any quieter bit after it
        while j < len(secs) and secs[j].kind == "breakdown":
            j += 1
        if j < len(secs) and secs[j].kind == "build":
            for k in range(i, j):
                secs[k].kind = "groove"
    secs = _copy_repeated_builds(secs, feats.bands)
    secs = _returning_grooves(secs, feats.bands, B, top_b)

    # Bass HITS WITH GAPS (a stuttering section) straight after a drop and straight
    # into the next one is building up to it, not a groove (a drop never goes straight
    # into a groove). E.g. T11 51-58 and 115-122: drop | hit, gap, hit... |
    # drop. (Looking past a 1-2 bar cut at the end of the first drop, like bar 50.)
    for i in range(1, len(secs) - 1):
        prev = i - 1
        if secs[prev].kind == "breakdown" and secs[prev].length <= 2 and prev > 0:
            prev -= 1
        if (secs[i].kind in ("groove", "breakdown") and secs[prev].kind == "drop"
                and secs[i + 1].kind == "drop" and secs[i].length >= 4 and stutters(secs[i])):
            secs[i].kind = "build"

    # STRAIGHT OUT OF A DROP IT BREAKS DOWN FIRST: an 8+ bar "build" right after a drop
    # (looking past a 1-2 bar cut) is half breakdown, half build. E.g. T10
    # 49-56 (49-52 / 53-56) and T6 65-72 (65-68 / 69-72). The halves are counted over
    # the build plus a 1-2 bar pre-drop after it (T6: build 65-71 + pre-drop 72). Not
    # for a build of bass HITS AT FULL LEVEL with gaps (T11 51-58: the whole
    # 8 bars build up; T10s hits sit ~13 dB under the drop, so they don't count).
    out = []
    for i, sec in enumerate(secs):
        p = i - 1
        if p > 0 and secs[p].kind in ("breakdown", "predrop") and secs[p].length <= 2:
            p -= 1
        span_end = sec.end
        if i + 1 < len(secs) and secs[i + 1].kind == "predrop" and secs[i + 1].length <= 2:
            span_end = secs[i + 1].end
        full_hits = stutters(sec) and float(bars(sec, B).max()) >= top_b - S["drop_bass_within_db"]
        if (sec.kind == "build" and span_end - sec.start >= 8 and p >= 0 and secs[p].kind == "drop"
                and not full_hits):
            mid = sec.start + (span_end - sec.start) // 2
            out += [Section(sec.start, mid, "breakdown"), Section(mid, sec.end, "build")]
        else:
            out.append(sec)
    secs = out

    # The OPENING GROOVE/INTRO COMING BACK straight after a drop, bar for bar, is a
    # breakdown: the drop is breaking down into it (a drop never goes straight into a
    # groove). E.g. T14 57-64 = 9-16 exactly (0.1 dB). It can pass for "the
    # drop with a part missing" (or be called a groove), so it's checked here. Only near-exact copies:
    # T10's drop dips are 2.6+ dB from its groove and stay drop.
    first_event = next((k for k, x in enumerate(secs) if x.kind in ("build", "predrop", "drop", "fakedrop")),
                       len(secs))
    opening = [x for x in secs[:first_event] if x.kind in ("intro", "groove")]
    for i in range(1, len(secs)):
        sec = secs[i]
        if sec.kind not in ("drop", "groove") or secs[i - 1].kind != "drop" or not 4 <= sec.length <= 16:
            continue
        for src in opening:
            if src.start + sec.length - 1 > src.end - 1 + 8:  # must mostly lie in the opening
                continue
            d = np.mean([np.sqrt(np.mean((feats.bands[sec.start - 1 + k] - feats.bands[src.start - 1 + k]) ** 2))
                         for k in range(sec.length)])
            if d < S["opening_copy_db"]:
                sec.kind = "breakdown"
                break

    # YOU CAN'T BREAK DOWN A BREAKDOWN (the user's rule): a breakdown follows a drop.
    # If a breakdown runs into a clearly different-sounding part (8+ bars, before any
    # build), that part isn't breaking down from anything: it's a GROOVE, up to the
    # build. E.g. T1: drop | 70-81 breakdown | 82-109 groove | build.
    # A 4-bar part counts too when the build comes straight after it: T14
    # drop | 57-64 breakdown | 65-68 groove | 69-72 build (the user, 2026-10-15).
    # (Runs after the opening-copy rule above, which is what finds T14's 57-64.)
    for i in range(1, len(secs)):
        sec, prev = secs[i], secs[i - 1]
        nxt_kind = secs[i + 1].kind if i + 1 < len(secs) else None
        # (the 4-bar case only after a breakdown that kept its bass at full level, like
        # T14's groove copy; T13's breakdown just fades further at 85-88)
        long_enough = sec.length >= 8 or (sec.length >= 4 and nxt_kind in ("build", "predrop")
                                          and level(prev, B) >= top_b - S["drop_bass_within_db"])
        if (sec.kind != "breakdown" or prev.kind != "breakdown" or not long_enough
                or _sound_distance(feats, prev, sec) < S["breakdown_then_groove_db"]):
            continue
        # the earlier breakdown must come straight from a drop (looking back over its pieces)
        k = i - 1
        while k > 0 and secs[k - 1].kind == "breakdown":
            k -= 1
        if k == 0 or secs[k - 1].kind != "drop":
            continue
        j = i
        while j < len(secs) and secs[j].kind == "breakdown":
            j += 1
        if j < len(secs) and secs[j].kind in ("build", "predrop"):
            for m in range(i, j):
                secs[m].kind = "groove"

    # A PRE-DROP ALWAYS COMES BEFORE A DROP (the user's rule). So a "breakdown" straight
    # after a pre-drop means the track faked us out: it's a FAKE DROP if another build
    # follows it (T6 33-36). Otherwise the "pre-drop" was really the end of the build.
    # (Looking past a 1-2 bar "drop" scrap where the bass slammed in, e.g. T6 bar 32:
    # that bar is still the pre-drop.)
    for i in range(1, len(secs)):
        if secs[i].kind != "breakdown":
            continue
        p = i - 1
        if p > 0 and secs[p].kind == "drop" and secs[p].length <= 2 and secs[p - 1].kind == "predrop":
            secs[p].kind = "predrop"
            p -= 1
        if secs[p].kind != "predrop":
            continue
        if i + 1 < len(secs) and secs[i + 1].kind == "build":
            secs[i].kind = "fakedrop"
        elif p > 0 and secs[p - 1].kind == "build":
            secs[p].kind = "build"

    secs = _merge_scraps(secs)

    # PRE-DROPS ARE 1, 2, 4 OR 8 BARS, never 3 (the user: "you rarely ever see 3 bar
    # phrases at all"; a 2-bar build + a 1-bar pre-drop adds up to 3, but neither is 3
    # on its own). An odd-length pre-drop is redrawn as 1, 2 or 4 bars ending where it
    # ends, starting on whichever of those bars the sound changes most (novelty); the
    # section before it gives or takes the difference. E.g. T6 30-32 -> 32 (change at
    # 32: 0.71, at 30: 0.10); T8 110-112 -> 109-112 (big change at 109: 1.38).
    nov = novelty(feats.bands)
    joined: list[Section] = []  # (neighbouring pre-drop pieces count as one pre-drop)
    for sec in secs:
        if joined and sec.kind == "predrop" and joined[-1].kind == "predrop":
            joined[-1].end = sec.end
        else:
            joined.append(sec)
    secs = joined
    for i in range(1, len(secs)):
        sec, prev = secs[i], secs[i - 1]
        if sec.kind != "predrop" or sec.length in S["predrop_lengths"]:
            continue
        options = [L for L in S["predrop_lengths"] if sec.end - L > prev.start]
        if not options:
            continue
        L = max(options, key=lambda L: nov[sec.end - L - 1])
        prev.end = sec.start = sec.end - L

    # A DROP RUNS IN 8-BAR PHRASES from where it lands, and the breakdown after it starts
    # on one of them. A breakdown starting halfway through one (4 bars off), whose first
    # bar still has the bass at drop level, really starts 4 bars later: the bars in
    # between are the end of the drop. Drops often pull the bass for the last bar or two
    # as an audible "end of the drop" (the user, 2026-10-15). E.g. T13: drop lands
    # on 17, bass cut at 79-80, breakdown from 81 (the grid had snapped it back to 77).
    # In all 14 labelled tracks, no breakdown starts 4 bars off its drop's 8-bar grid.
    for i in range(1, len(secs)):
        sec = secs[i]
        if sec.kind != "breakdown" or secs[i - 1].kind != "drop" or sec.length <= 4:
            continue
        k = i - 1  # where the drop landed (looking back over its pieces)
        while k > 0 and secs[k - 1].kind == "drop":
            k -= 1
        if (sec.start - secs[k].start) % 8 == 4 and B[sec.start - 1] >= top_b - S["drop_bass_within_db"]:
            secs[i - 1].end = sec.start = sec.start + 4

    # SILENCE AT THE END is the outro, however short (it's usually too short to be its
    # own phrase and gets merged into the last drop otherwise). E.g. T14 ends on
    # a drop then 2 silent bars; T13 fades out over 4.
    last_sound = n_bars
    while last_sound >= 1 and E[last_sound - 1] <= -S["silence_db"]:
        last_sound -= 1
    first_silent = last_sound + 1
    if 1 < first_silent <= n_bars:
        out: list[Section] = []
        for sec in secs:
            if sec.start >= first_silent:
                out.append(Section(sec.start, sec.end, "outro"))
            elif sec.end > first_silent:
                out += [Section(sec.start, first_silent, sec.kind), Section(first_silent, sec.end, "outro")]
            else:
                out.append(sec)
        secs = out

    # A BUILD ALWAYS LEADS INTO SOMETHING: the part straight after a build is never
    # the outro, even after the last drop. If the outro would start right after a
    # build, that first piece is a BREAKDOWN and the outro starts at the next phrase
    # (the user, 2026-10-15). E.g. T12: drop 2 to 168 | build 169-172 | breakdown
    # 173-188 (bass back near full) | outro 189-197 (fading out).
    for i in range(len(secs) - 2):
        if secs[i].kind == "build" and secs[i + 1].kind == "outro" and secs[i + 2].kind == "outro":
            secs[i + 1].kind = "breakdown"
    return secs


def _returning_grooves(secs: list[Section], bands: np.ndarray, B: np.ndarray, top_b: float) -> list[Section]:
    """
    The opening part (intro/groove, with the bass in) often comes back later, bar for
    bar, after a breakdown. Mid-track it isn't an intro any more: it's a GROOVE (Fuck
    Around 113-124 = bars 13-24; T6 113-120 = 17-24; T5 65-78 = 17-30).
    So: if the end of a "breakdown" is a near-exact copy of opening bars that have the
    bass in, those bars are a groove. (Copies of a bass-less intro stay breakdown, e.g.
    T3 85-92, and so do looser copies, e.g. T2 81-88 at ~3 dB.)
    """
    S = SETTINGS
    if not secs:
        return secs
    # the opening: everything before the first build / drop / fake drop
    opening_end = next((s.start for s in secs if s.kind in ("build", "predrop", "drop", "fakedrop")), 1)
    if opening_end <= 1:
        return secs

    def bar_distance(a: int, b: int) -> float:
        return float(np.sqrt(np.mean((bands[a - 1] - bands[b - 1]) ** 2)))

    out: list[Section] = []
    for sec in secs:
        if sec.kind != "breakdown" or sec.start <= opening_end:
            out.append(sec)
            continue
        best_run = 0
        for src_end in range(S["groove_copy_min_bars"], opening_end):  # source bar for sec.end - 1
            lag = (sec.end - 1) - src_end
            run, total = 0, 0.0  # matching bars counted back from the section's end
            for b in range(sec.end - 1, sec.start - 1, -1):
                src = b - lag
                if src < 1:
                    break
                d = bar_distance(b, src)
                if d >= S["groove_copy_bar_db"]:
                    break
                run, total = run + 1, total + d
            # (bass checked over the whole copied stretch, not bar by bar: some grooves
            # have an on/off stutter bass, e.g. T6. Its upper quarter, not the typical
            # bar: in a groove of bass HITS with gaps the typical bar is a gap - Enter the
            # T11 19-26, back at 83-90)
            src = B[sec.end - 1 - run - lag:sec.end - 1 - lag]
            src_bass = float(np.percentile(src, 75)) if run else -999.0
            if (run >= S["groove_copy_min_bars"] and total / run < S["groove_copy_mean_db"]
                    and src_bass >= top_b - S["intro_bass_in_db"]):
                best_run = max(best_run, run)
        if best_run:
            split = sec.end - best_run
            if split > sec.start:
                out.append(Section(sec.start, split, "breakdown"))
            out.append(Section(split, sec.end, "groove"))
        else:
            out.append(sec)
    return out


def _copy_repeated_builds(secs: list[Section], bands: np.ndarray) -> list[Section]:
    """
    Tracks often repeat a build-up (and its pre-drop) bar for bar later on. Once we've
    found the first one, a later stretch that sounds the same bar for bar is that
    build coming back, so it gets the same labels, bar by bar. (Often the second copy
    is harder to spot by itself, e.g. it comes straight out of a drop.)
    Only builds/pre-drops are copied: other kinds depend more on what's around them.
    """
    S = SETTINGS
    if not secs:
        return secs
    n_bars = secs[-1].end - 1
    kind = [""] * (n_bars + 2)  # kind of each bar, 1-based
    for s in secs:
        for b in range(s.start, s.end):
            kind[b] = s.kind
    copied = kind[:]

    def bar_distance(a: int, b: int) -> float:
        return float(np.sqrt(np.mean((bands[a - 1] - bands[b - 1]) ** 2)))

    src_kinds = ("build", "predrop")
    # Each build-up = a run of build bars (back-to-back build sections count as one)
    # plus the pre-drop straight after it, if any: [start, end) in bars.
    builds: list[tuple[int, int]] = []
    b = 1
    while b <= n_bars:
        if kind[b] != "build":
            b += 1
            continue
        start = b
        while b <= n_bars and kind[b] == "build":
            b += 1
        if b - start >= S["repeat_min_bars"]:
            end = b
            while end <= n_bars and kind[end] == "predrop":
                end += 1
            builds.append((start, end))

    for start, end in builds:
        span = end - start
        for lag in range(max(span, 8), n_bars, S["grid_bars"]):
            if end - 1 + lag > n_bars:
                break
            targets = range(start + lag, end + lag)
            if sum(kind[b] in src_kinds for b in targets) * 2 >= span:
                continue  # already (mostly) a build there
            if kind[end + lag] not in ("drop", "fakedrop"):
                continue  # a build has to lead into a drop
            dist = np.mean([bar_distance(b, b - lag) for b in targets])
            if dist < S["repeat_bar_db"]:
                for b in targets:
                    copied[b] = kind[b - lag]

    if copied == kind:
        return secs
    # rebuild the sections from the per-bar kinds
    out: list[Section] = []
    for b in range(1, n_bars + 1):
        if out and out[-1].kind == copied[b]:
            out[-1].end = b + 1
        else:
            out.append(Section(b, b + 1, copied[b]))
    return out


def _keep_predrops_after_builds(secs: list[Section]) -> list[Section]:
    """
    What a short bass cut right before a drop is called depends on what came before it:
        after a BUILD      -> pre-drop (it separates the build from the drop)
        after a DROP       -> breakdown (a short break between two drops)
        after a GROOVE/INTRO -> build (it's what builds into the drop)
        after a BREAKDOWN  -> just the end of that breakdown (folded back in)
    """
    after = {"drop": "breakdown", "groove": "build", "intro": "build"}
    out: list[Section] = []
    for sec in secs:
        if sec.kind == "predrop" and out and out[-1].kind != "build":
            if out[-1].kind in after:
                sec.kind = after[out[-1].kind]
                out.append(sec)
            else:
                out[-1].end = sec.end
        else:
            out.append(sec)
    if out and out[0].kind == "predrop":  # a cut at the very start of the track
        out[0].kind = "intro"
    # ...and the other way round: a short quiet bit sitting between a build and a
    # drop (or fake drop) IS a pre-drop, whatever it was called before.
    for i in range(1, len(out) - 1):
        if (out[i].kind in ("breakdown", "groove", "intro") and out[i - 1].kind == "build"
                and out[i + 1].kind in ("drop", "fakedrop")
                and out[i].length <= SETTINGS["predrop_max_bars"]):
            out[i].kind = "predrop"
    return out


def _merge_scraps(secs: list[Section]) -> list[Section]:
    """
    Sections shorter than min_section_bars (except pre-drops, which really can be one
    bar) are noise: fold them into the section before (or after, at the very start).
    """
    S = SETTINGS
    out: list[Section] = []
    for sec in secs:
        if sec.length < S["min_section_bars"] and sec.kind != "predrop" and out:
            out[-1].end = sec.end
        else:
            out.append(sec)
    if len(out) > 1 and out[0].length < S["min_section_bars"] and out[0].kind != "predrop":
        out[1].start = out[0].start
        out = out[1:]
    return out


def _split_off_predrops(secs: list[Section], B: np.ndarray, arrivals: set[int] = frozenset(),
                        E: Optional[np.ndarray] = None) -> list[Section]:
    """
    Find pre-drops bar by bar: bars right before (or right at the start of) a drop / fake
    drop where the bass is cut well below what it was just before. They're split off into
    their own "predrop" section, so the drop starts on its first full bar.
    If the overall loudness E is given, a bar that gets clearly LOUDER than the build
    before it isn't a cut, even if its bass dips (it's the build peaking).
    """
    S = SETTINGS
    n_bars = len(B)
    out: list[Section] = []
    for i, sec in enumerate(secs):
        # (a groove that comes back after a short break gets its cut split off too)
        # Also every section where the bass slams back in, even straight after another
        # drop (e.g. drop | 2-bar cut | drop 2).
        starts_run = sec.kind in ("drop", "fakedrop", "groove") and (
            i == 0 or secs[i - 1].kind != sec.kind or sec.start in arrivals)
        if not starts_run or sec.start <= 2:
            out.append(sec)
            continue
        # Bass level of the build-up before it (ignoring the last couple of bars)
        far, near = S["predrop_ref_bars"]
        ref_lo, ref_hi = max(1, sec.start - far), max(1, sec.start - near)
        if ref_hi <= ref_lo:
            out.append(sec)
            continue
        ref = float(np.median(B[ref_lo - 1:ref_hi]))
        ref_e = float(np.median(E[ref_lo - 1:ref_hi])) if E is not None else None

        def is_cut(bar: int) -> bool:
            if not (1 <= bar <= n_bars and B[bar - 1] <= ref - S["predrop_cut_db"]):
                return False
            return ref_e is None or E[bar - 1] <= ref_e + S["predrop_max_louder_db"]


        cut_start, cut_end = sec.start, sec.start  # cut bars: [cut_start, cut_end)
        while is_cut(cut_end) and cut_end - cut_start < S["predrop_max_bars"] and cut_end < sec.end - 1:
            cut_end += 1  # cut bars at the very start of the drop
        prev = out[-1] if out else None
        while (prev is not None and is_cut(cut_start - 1) and cut_start - 1 >= prev.start
               and cut_end - cut_start < S["predrop_max_bars"]):
            cut_start -= 1  # cut bars at the end of the section before (may use it all up)

        if cut_end > cut_start:
            if prev is not None and cut_start < prev.end:
                prev.end = cut_start
            out.append(Section(cut_start, cut_end, "predrop"))
            sec = Section(cut_end, sec.end, sec.kind)
        out.append(sec)
    return [s for s in out if s.length > 0]


def _numbered_labels(sections: list[Section]) -> list[str]:
    """
    Labels with numbers where a kind appears in several separate places:
    "Drop 1", "Drop 2", "Build 1"... (Intro and Outro are never numbered.)
    Back-to-back sections of the same kind share a label (they get merged).
    """
    runs: list[list[int]] = []
    for i, s in enumerate(sections):
        if runs and sections[runs[-1][0]].kind == s.kind:
            runs[-1].append(i)
        else:
            runs.append([i])
    totals: dict[str, int] = {}
    for r in runs:
        totals[sections[r[0]].kind] = totals.get(sections[r[0]].kind, 0) + 1
    seen: dict[str, int] = {}
    labels = [""] * len(sections)
    for r in runs:
        k = sections[r[0]].kind
        seen[k] = seen.get(k, 0) + 1
        name = KIND_NAMES[k]
        base = name if totals[k] == 1 or k in ("intro", "outro") else f"{name} {seen[k]}"
        for i in r:
            labels[i] = base
    return labels


def label_sections(starts: list[int], n_bars: int, feats: BarFeatures) -> list[tuple[str, str]]:
    """
    Name each section from `starts` -> [(kind, label), ...], one per start.
    (Kept for the tests and for simple use; detect_phrases uses classify_sections
    directly, because pre-drops can add sections.)
    """
    secs = classify_sections(starts, n_bars, feats)
    labels = _numbered_labels(secs)
    out = []
    for s in starts:
        i = max(j for j, sec in enumerate(secs) if sec.start <= s)
        out.append((secs[i].kind, labels[i]))
    return out


# --------------------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------------------


def detect_phrases_from_samples(samples: np.ndarray, sr: int, grid: Beatgrid) -> list[Phrase]:
    """Full pipeline on already-decoded audio. Returns Phrase objects (source "detected")."""
    if grid.bar_count < 2:
        return []
    return detect_phrases_from_features(bar_features(samples, sr, grid), grid.bar_count)


def detect_phrases_from_features(feats: BarFeatures, bar_count: int) -> list[Phrase]:
    """
    Everything after measuring the bars: find the phrase grid, name the sections.
    (Split out so tuning experiments can re-run it on saved measurements in seconds.)
    """
    nov = novelty(feats.bands)
    for a in find_arrivals(feats):  # drops landing pull the phrase grid onto them
        nov[a - 1] += SETTINGS["arrival_novelty_bonus"]
    starts = choose_boundaries(nov)
    secs = classify_sections(starts, bar_count, feats)
    labels = _numbered_labels(secs)
    phrases: list[Phrase] = []
    for sec, label in zip(secs, labels):
        if phrases and phrases[-1].label == label:
            phrases[-1].length_bars += sec.length  # same section continues: merge
        else:
            phrases.append(Phrase(kind=sec.kind, label=label, start_bar=float(sec.start),
                                  length_bars=float(sec.length), source="detected"))
    return phrases


def detect_phrases(audio_path: Path, grid: Beatgrid) -> list[Phrase]:
    """Decode a file and detect its phrases. Raises RuntimeError if decoding fails."""
    sr = SETTINGS["sample_rate"]
    return detect_phrases_from_samples(decode_mono(audio_path, sr), sr, grid)
