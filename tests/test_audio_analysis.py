"""
Tests for our EDM phrase detection, using SYNTHETIC audio with a known structure.

The fake track (174 BPM, 128 bars):
    Intro 16 | Build 8 | Drop 32 | Breakdown 16 | Build 8 | Drop 32 | Outro 16
"""

import wave

import numpy as np
import pytest

from djplanner import audio_analysis as aa
from djplanner.beatgrid import Beatgrid

SR = aa.SETTINGS["sample_rate"]
BPM = 174
STRUCTURE = [("intro", 16), ("build", 8), ("drop", 32), ("breakdown", 16),
             ("build", 8), ("drop", 32), ("outro", 16)]


def synth_track(structure=STRUCTURE, bpm=BPM, seed=1):
    """Make audio whose sections sound like EDM sections (roughly!)."""
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    spb = int(round(beat * SR))  # samples per beat
    t_beat = np.arange(spb) / SR
    hat = rng.normal(0, 1, spb) * np.exp(-t_beat * 60)  # short noise click
    hat = np.diff(hat, prepend=0)  # crude high-pass: hats live in the highs
    kick = np.sin(2 * np.pi * 55 * t_beat) * np.exp(-t_beat * 8)
    snare = rng.normal(0, 1, spb) * np.exp(-t_beat * 20)

    out = []
    for kind, bars in structure:
        for bar in range(bars):
            for b in range(4):
                x = np.zeros(spb)
                x += 0.15 * hat  # hats everywhere
                if kind in ("intro", "outro"):
                    x += 0.05 * np.sin(2 * np.pi * 440 * t_beat)
                if kind == "build":
                    ramp = (bar * 4 + b) / (bars * 4)
                    x += (0.1 + 0.4 * ramp) * snare
                if kind == "breakdown":
                    x += 0.25 * (np.sin(2 * np.pi * 220 * t_beat) + np.sin(2 * np.pi * 330 * t_beat))
                if kind == "drop":
                    x += 0.9 * kick
                    x += 0.5 * np.sin(2 * np.pi * 50 * (t_beat + b * beat))  # sustained sub
                    if b in (1, 3):
                        x += 0.4 * snare
                out.append(x)
    return np.concatenate(out).astype(np.float32)


def grid_for(structure=STRUCTURE, bpm=BPM):
    return Beatgrid.constant(bpm, bars=sum(b for _, b in structure))


# ----- the full pipeline on synthetic audio --------------------------------------------


def test_detects_known_structure():
    phrases = aa.detect_phrases_from_samples(synth_track(), SR, grid_for())
    got = [(p.start_bar, p.kind) for p in phrases]
    assert got == [(1, "intro"), (17, "build"), (25, "drop"), (57, "breakdown"),
                   (73, "build"), (81, "drop"), (113, "outro")]
    assert [p.label for p in phrases] == ["Intro", "Build 1", "Drop 1", "Breakdown",
                                          "Build 2", "Drop 2", "Outro"]
    assert all(p.source == "detected" for p in phrases)
    assert sum(p.length_bars for p in phrases) == 128


def test_intro_straight_into_drop_stays_intro():
    structure = [("intro", 16), ("drop", 32), ("outro", 16)]
    phrases = aa.detect_phrases_from_samples(synth_track(structure), SR, grid_for(structure))
    assert [(p.start_bar, p.kind) for p in phrases] == [(1, "intro"), (17, "drop"), (49, "outro")]
    assert [p.label for p in phrases] == ["Intro", "Drop", "Outro"]


def test_phrasing_offset_from_bar_1_is_found():
    # A 2-bar pickup before the real intro: phrases start on bars 3, 19, 51...
    structure = [("intro", 2), ("intro", 16), ("drop", 32), ("outro", 16)]
    phrases = aa.detect_phrases_from_samples(synth_track(structure), SR, grid_for(structure))
    starts = [p.start_bar for p in phrases]
    assert 19 in starts and 51 in starts


# ----- the pieces, with hand-made numbers -------------------------------------------------


def test_choose_boundaries_prefers_strong_changes_on_the_grid():
    nov = np.zeros(48)
    nov[16] = 1.0  # strong change at bar 17
    nov[32] = 0.9  # and at bar 33
    nov[21] = 1.0  # off-grid change (bar 22): ignored
    assert aa.choose_boundaries(nov) == [1, 17, 33]


def test_choose_boundaries_splits_overlong_sections():
    nov = np.zeros(100)  # nothing changes at all
    bounds = aa.choose_boundaries(nov)
    lengths = np.diff(bounds + [101])
    assert lengths.max() <= aa.SETTINGS["max_phrase_bars"]


def make_feats(sections):
    """
    Fake per-bar features from [(length, energy, bass, highs_start, highs_end), ...]
    (dB relative to the loudest parts). The 12-band "fingerprint" is built from the
    same numbers so different-sounding sections really do differ.
    """
    E, B, H = [], [], []
    for length, e, b, h0, h1 in sections:
        E += [e] * length
        B += [b] * length
        H += list(np.linspace(h0, h1, length))
    E, B, H = map(np.array, (E, B, H))
    bands = np.stack([E + (B if k < 4 else 0) + (H if k >= 8 else 0) for k in range(12)], axis=1)
    return aa.BarFeatures(energy=E, bass=B, highs=H, bands=bands)


def starts_of(sections):
    return list(np.cumsum([0] + [sec[0] for sec in sections[:-1]]) + 1)


def classify(sections):
    secs = aa.classify_sections(starts_of(sections), sum(x[0] for x in sections), make_feats(sections))
    return [(x.kind, x.start, x.length) for x in secs]


def test_label_sections_names_and_numbers():
    sections = [(8, -8, -40, -6, -6),     # intro: no bass
                (8, -3, -4, -8, -2),      # build: highs rising
                (16, 0, 0, 0, 0),         # drop
                (16, -8, -40, -6, -6),    # breakdown
                (8, -3, -4, -8, -2),      # build
                (16, 0, 0, 0, 0),         # drop
                (8, -8, -40, -6, -6)]     # outro
    named = aa.label_sections(starts_of(sections), 80, make_feats(sections))
    assert [label for _, label in named] == ["Intro", "Build 1", "Drop 1", "Breakdown", "Build 2", "Drop 2", "Outro"]


def test_long_drop_in_two_parts_is_one_drop():
    sections = [(16, -8, -40, -6, -6), (16, 0, 0, 0, 0), (16, -0.5, 0, 1, 1), (16, -8, -40, -6, -6)]
    named = aa.label_sections(starts_of(sections), 64, make_feats(sections))
    assert [label for _, label in named] == ["Intro", "Drop", "Drop", "Outro"]


def test_rising_intro_is_still_an_intro():
    # opening 16 bars with a filter opening up, then a drop: intro, not build
    assert classify([(16, -6, -20, -10, -2), (16, 0, 0, 0, 0)])[0][0] == "intro"


def test_predrop_and_arrival_found_off_the_grid():
    # intro 8 | build 8 | bass cut for 2 bars | drop from bar 19 (off the 4-bar grid)
    got = classify([(8, -8, -40, -6, -6), (8, -3, -4, -8, -2), (2, -10, -40, -6, -6), (22, 0, 0, 0, 0)])
    assert ("predrop", 17, 2) in got and ("drop", 19, 22) in got


def test_fake_drop_between_two_builds():
    # T4 pattern: intro | build | pre-drop | FAKE drop | build | pre-drop | drop
    got = classify([(16, -3, -1, -6, -6), (7, -7, -22, -4, 0), (1, -9, -40, -1, -1),
                    (4, -3, -2, -7, -6), (4, -1, 0, -4, 3), (2, -3, -15, -1, -1), (16, 0, 0, 0, 0)])
    assert [g[0] for g in got] == ["intro", "build", "predrop", "fakedrop", "build", "predrop", "drop"]
    assert ("fakedrop", 25, 4) in got and ("drop", 35, 16) in got


def test_short_bass_cut_without_build_is_not_a_fake_drop():
    # intro, the bass drops in (an "arrival") but no build-up before it: just the track
    got = classify([(8, -8, -40, -6, -6), (8, -3, -2, -6, -6), (16, 0, 0, 0, 0)])
    assert "fakedrop" not in [g[0] for g in got]


def test_too_short_track_gives_nothing():
    assert aa.detect_phrases_from_samples(np.zeros(1000, np.float32), SR, Beatgrid.constant(174, 1)) == []


# ----- decoding with ffmpeg ------------------------------------------------------------------


@pytest.mark.skipif(aa.find_ffmpeg() is None, reason="ffmpeg not installed")
def test_decode_wav_file(tmp_path):
    tone = (np.sin(2 * np.pi * 440 * np.arange(44100) / 44100) * 20000).astype(np.int16)
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(tone.tobytes())
    before = path.read_bytes()
    samples = aa.decode_mono(path)
    assert abs(len(samples) - SR) < 50  # 1 second at the analysis rate
    assert 0.5 < np.abs(samples).max() < 0.7  # 20000/32768
    assert path.read_bytes() == before  # file untouched


@pytest.mark.skipif(aa.find_ffmpeg() is None, reason="ffmpeg not installed")
def test_decode_bad_file_raises(tmp_path):
    bad = tmp_path / "not_audio.mp3"
    bad.write_text("hello")
    with pytest.raises(RuntimeError):
        aa.decode_mono(bad)


def test_short_dip_inside_drop_is_part_of_the_drop():
    # drop 16 | 8 bars with the top end (e.g. vocals) out, bass still going | drop 16 |
    # breakdown 16 | drop 16. Drops keep changing to stay interesting: still the drop
    # (T10). (Before 2026-10-14 this test used an 8-bar BASS cut; the user
    # then said a bassline dropping out is a breakdown - see the next test.)
    sections = [(16, 0, 0, 0, 0), (8, -2, 0, -4, -4), (16, 0, 0, 0, 0), (16, -8, -40, -6, -6), (16, 0, 0, 0, 0)]
    named = aa.label_sections(starts_of(sections), 72, make_feats(sections))
    assert [label for _, label in named] == ["Drop 1", "Drop 1", "Drop 1", "Breakdown", "Drop 2"]


def test_bassline_dropping_out_inside_a_drop_is_a_breakdown():
    # drop 16 | 8 bars at nearly full level but the bass band 10 dB down | drop 16.
    # The user (T11 75-82): "the bassline in the drop disappears ... hence
    # why it's a breakdown".
    sections = [(16, 0, 0, 0, 0), (8, -1, -10, 0, 0), (16, 0, 0, 0, 0)]
    got = classify(sections)
    assert ("breakdown", 17, 8) in got


def test_predrop_after_breakdown_is_part_of_the_breakdown():
    # drop | breakdown | bass cut 2 bars | drop: no build, so no pre-drop
    got = classify([(16, 0, 0, 0, 0), (14, -8, -10, -6, -6), (2, -10, -40, -6, -6), (16, 0, 0, 0, 0)])
    assert "predrop" not in [g[0] for g in got]
    assert ("breakdown", 17, 16) in got and ("drop", 33, 16) in got


def test_short_gap_between_build_and_drop_is_a_predrop():
    S = aa.Section
    out = aa._keep_predrops_after_builds([S(1, 9, "intro"), S(9, 17, "build"), S(17, 19, "breakdown"),
                                          S(19, 41, "drop"), S(41, 57, "breakdown"), S(57, 59, "predrop"),
                                          S(59, 75, "drop")])
    assert [(x.kind, x.start, x.length) for x in out] == [
        ("intro", 1, 8), ("build", 9, 8), ("predrop", 17, 2), ("drop", 19, 22),
        ("breakdown", 41, 18),  # the "pre-drop" after a breakdown is folded into it
        ("drop", 59, 16)]


def test_section_with_bass_straight_after_a_drop_is_a_breakdown_not_a_groove():
    # drop | 16 bars with the beat + bass still going but below drop level | drop
    # The user's rule: a drop never goes straight into a groove, so it's a breakdown.
    got = classify([(8, -8, -40, -6, -6), (8, -3, -4, -8, -2), (16, 0, 0, 0, 0),
                    (16, -4, -5, -12, -12), (16, 0, 0, 0, 0)])
    assert {bar_kinds(got)[b] for b in range(33, 49)} == {"breakdown"}


def test_quiet_part_after_drop_and_cut_is_one_breakdown():
    # T8 pattern: drop | quiet 16 bars with some bass (no drums, highs down) |
    # build | drop. Not a groove (straight after a drop, and not full level).
    got = classify([(8, -8, -40, -6, -6), (8, -3, -4, -8, -2), (16, 0, 0, 0, 0),
                    (16, -7, -6, -16, -16), (8, -3, -4, -8, -2), (16, 0, 0, 0, 0)])
    assert {bar_kinds(got)[b] for b in range(33, 49)} == {"breakdown"}


def bar_kinds(got):
    """classify() output -> {bar: kind}"""
    return {b: kind for kind, start, length in got for b in range(start, start + length)}


def feats_from_bars(E, B, H):
    """Like make_feats, but from per-bar lists (for ramps that make_feats can't do)."""
    E, B, H = (np.asarray(x, dtype=float) for x in (E, B, H))
    bands = np.stack([E + (B if k < 4 else 0) + (H if k >= 8 else 0) for k in range(12)], axis=1)
    return aa.BarFeatures(energy=E, bass=B, highs=H, bands=bands)


def test_build_that_swells_in_loudness_with_flat_highs():
    # T8 pattern: intro | 16 bars getting louder (highs flat) | drop.
    # Too long to count as a "short lead-in", and the highs don't rise: it's the
    # loudness climbing that makes it a build.
    E = [-8] * 16 + list(np.linspace(-12, -3, 16)) + [0] * 16
    B = [-40] * 16 + [-6] * 16 + [0] * 16
    H = [-3] * 16 + [-3] * 16 + [0] * 16
    secs = aa.classify_sections([1, 17, 33], 48, feats_from_bars(E, B, H))
    assert [(s.kind, s.start) for s in secs] == [("intro", 1), ("build", 17), ("drop", 33)]


def test_scrap_bar_at_start_then_beat_is_an_intro_not_a_build():
    # T7 pattern: 1 near-silent bar, then the beat + bass slam in at bar 2
    # for 8 bars, then the full drop. Bars 2-9 are the intro (a DJ intro with beats).
    E = [-40] + [-3] * 8 + [0] * 16
    B = [-80] + [-3] * 8 + [0] * 16
    H = [-40] + [-1] * 8 + [0] * 16
    secs = aa.classify_sections([1, 2, 10], 25, feats_from_bars(E, B, H))
    assert [(s.kind, s.start, s.length) for s in secs] == [("intro", 1, 9), ("drop", 10, 16)]


def test_full_level_part_after_intro_with_no_build_is_a_groove():
    # T7 pattern: intro (with bass) | full level, but nothing builds into it
    # | eases off | build | drop. No build-up before it, so it's not a drop: groove.
    got = bar_kinds(classify([(8, -4, -6, -3, -3), (8, 0, 0, 0, 0), (8, -3, -3, -2, -2),
                              (8, -6, -12, -10, 0), (16, 0, 0, 0, 0)]))
    assert {got[b] for b in range(9, 25)} == {"groove"}
    assert got[25] == "build" and got[33] == "drop"


# T4 pattern: intro | bass slams in at FULL level (beat going, not as loud
# overall as the drop) | build | pre-drop | drop
T4_START = [(8, -7, -40, -6, -6), (8, -3, -1, -6, -6), (7, -7, -22, -4, 0),
              (1, -9, -40, -1, -1), (16, 0, 0, 0, 0)]


def test_full_bass_after_intro_is_a_groove():
    got = bar_kinds(classify(T4_START))
    assert {got[b] for b in range(1, 9)} == {"intro"}
    assert {got[b] for b in range(9, 17)} == {"groove"}
    assert got[17] == "build" and got[25] == "drop"


def test_bass_carrying_on_after_it_came_in_is_a_groove():
    # T2 pattern: 16-bar intro with the bass already in (a few dB under the drop)
    # | 8 bars with the bass at least as strong | build | drop. Bars 17-24: groove.
    got = bar_kinds(classify([(16, -4, -4.7, -6, -6), (8, -3, -3.7, -4, -4),
                              (7, -7, -22, -4, 0), (1, -9, -40, -1, -1), (16, 0, 0, 0, 0)]))
    assert {got[b] for b in range(1, 17)} == {"intro"}
    assert {got[b] for b in range(17, 25)} == {"groove"}


def test_bass_dropping_back_after_it_came_in_is_still_intro():
    # T8 pattern: no bass | bass comes in (a few dB under the drop) | the bass
    # drops back down | build | drop. All intro before the build.
    got = bar_kinds(classify([(8, -10, -60, -1, -1), (16, -3, -3.3, -1, -1), (8, -5, -6.5, -2, -2),
                              (7, -7, -22, -4, 0), (1, -9, -40, -1, -1), (16, 0, 0, 0, 0)]))
    assert {got[b] for b in range(1, 33)} == {"intro"}


def test_opening_groove_coming_back_after_a_drop():
    # ...drop | what LOOKS like build + pre-drop + fake drop, but the "fake drop" sounds
    # just like the opening groove: it's the groove coming back, and the bit before it
    # (straight after the drop) is a breakdown.
    got = bar_kinds(classify(T4_START + [(7, -5, -8, -8, -2), (1, -9, -40, -1, -1),
                                           (8, -3, -1, -6, -6), (7, -7, -22, -4, 0),
                                           (1, -9, -40, -1, -1), (16, 0, 0, 0, 0)]))
    assert {got[b] for b in range(41, 49)} == {"breakdown"}
    assert {got[b] for b in range(49, 57)} == {"groove"}
    assert got[57] == "build" and got[65] == "drop"


def test_breakdown_then_bright_build_after_a_drop():
    # T1 bars 42-53 (real numbers): drop | 8 quiet bars, highs creeping
    # up a bit | 4 bars, highs jump ~9 dB and the bass climbs into the drop | drop.
    # The 8 bars are the breakdown (not part of the build), and the last bar of the
    # climb (nearly full level) is still the build: the drop starts after it.
    E = [-8] * 8 + [0] * 16 + [-12, -17, -12, -12, -8, -6, -6, -6] + [-6, -6, -4, -0.4] + [0] * 16
    B = [-40] * 8 + [0] * 16 + [-23, -30, -22, -22, -23, -26, -22, -19] + [-12, -11, -6.6, -1.9] + [0] * 16
    H = [-6] * 8 + [0] * 16 + [-5, -15, -14, -14, -8, -9, -5, -8] + [0.5, 0.4, 0.7, -1.1] + [0] * 16
    secs = aa.classify_sections([1, 9, 25, 33, 37], 52, feats_from_bars(E, B, H))
    assert [(s.kind, s.start, s.length) for s in secs] == [
        ("intro", 1, 8), ("drop", 9, 16), ("breakdown", 25, 8), ("build", 33, 4), ("drop", 37, 16)]


def test_four_bar_build_that_gets_louder_with_a_bass_dip_at_the_end():
    # T9 bars 21-33 (real numbers): intro | 4 bars that sound different, bass
    # thinning but LOUDER overall, last bar's bass dips further | drop. All 4 bars are
    # the build: the bass dip at the end isn't a pre-drop cut (it gets louder, not quieter).
    E = [-4.5] * 28 + [-3.0, -3.8, -1.9, -1.1] + [0] * 16
    B = [-8.2] * 28 + [-10.9, -11.3, -9.0, -16.3] + [0] * 16
    H = [-1.0] * 28 + [-5.3, -6.0, 0.2, 1.6] + [0] * 16
    feats = feats_from_bars(E, B, H)
    feats.bands[28:32, 4:8] += 8.0  # the build has its own sound (a riser in the mids), as in the real track
    secs = aa.classify_sections([1, 17, 33], 48, feats)
    assert [(s.kind, s.start, s.length) for s in secs] == [("intro", 1, 28), ("build", 29, 4), ("drop", 33, 16)]


def test_long_stretch_starting_quiet_is_a_drop_not_a_build():
    # T9 bars 129-192: the drop opens with 2 quiet bars, which makes its highs
    # "rise" over the section - but a 64-bar build doesn't exist: it's the drop.
    E = [-8] * 8 + [-1.5] * 8 + [-6, -7] + [0] * 62
    B = [-40] * 8 + [-10] * 8 + [-5, -11] + [0] * 62
    H = [-6] * 8 + [-6, -6, -5, -4, -3, -2, -1, 0] + [-11, -17] + [-1] * 62
    secs = aa.classify_sections([1, 9, 17], 80, feats_from_bars(E, B, H))
    assert secs[-1].kind == "drop" and secs[-1].end == 81 and secs[-1].start <= 19


def test_opening_coming_back_after_a_breakdown_is_a_groove():
    # T9 pattern: the intro (bass in) comes back bar for bar after the
    # breakdown: those bars are a groove, not more breakdown.
    S = aa.Section
    rng = np.random.default_rng(5)
    bands = rng.normal(0, 8, size=(64, 12))
    bands[40:48] = bands[0:8]  # bars 41-48 = bars 1-8 (the intro)
    B = np.array([-8.0] * 64)  # bass in throughout (a few dB under the drop)
    secs = [S(1, 9, "intro"), S(9, 13, "build"), S(13, 33, "drop"), S(33, 49, "breakdown"),
            S(49, 53, "build"), S(53, 65, "drop")]
    out = aa._returning_grooves(secs, bands, B, top_b=0.0)
    assert [(s.kind, s.start, s.length) for s in out] == [
        ("intro", 1, 8), ("build", 9, 4), ("drop", 13, 20), ("breakdown", 33, 8),
        ("groove", 41, 8), ("build", 49, 4), ("drop", 53, 12)]
    # ...but a copy of a BASS-LESS intro stays a breakdown (T3 pattern)
    out = aa._returning_grooves(secs, bands, np.array([-60.0] * 64), top_b=0.0)
    assert [s.kind for s in out] == [s.kind for s in secs]


def test_stripped_part_then_two_bar_swell_is_breakdown_then_build():
    # T10 bars 9-25 (real numbers): groove | 6 bars stripped back (bass gone,
    # flat, ~9 dB quieter) | 2 bars swelling (louder, bass partly back) | drop.
    # The user (2026-10-14): half breakdown, half build (17-20 / 21-24). (This test used
    # to expect 17-22 / 23-24.)
    E = [-4] * 8 + [-3.4] * 8 + [-9.5, -9.1, -9.8, -9.3, -9.2, -9.5, -6.9, -7.8] + [0] * 16
    B = [-4.5] * 8 + [-4.8] * 8 + [-41, -52, -44, -56, -41, -51, -25.5, -27.8] + [0] * 16
    H = [-3.5] * 8 + [-0.2] * 8 + [-7, -4.4, -3.7, -3, -4.7, -5, -4.6, -2.5] + [0] * 16
    got = bar_kinds([(s.kind, s.start, s.length) for s in
                     aa.classify_sections([1, 9, 17, 25], 40, feats_from_bars(E, B, H))])
    assert {got[b] for b in range(17, 21)} == {"breakdown"}
    assert {got[b] for b in range(21, 25)} == {"build"} and got[25] == "drop"


def test_two_bar_build_at_the_end_of_the_intro():
    # T1 pattern: long intro with bass | the highs open up for the last
    # 2 bars | drop. The phrase grid only splits at the drop; the 2-bar build-up is
    # found inside the intro.
    sections = [(23, -3, -3, -3.3, -3.3), (2, -1.7, -3.5, 0.3, 0.3), (16, 0, 0, 0, 0)]
    secs = aa.classify_sections([1, 26], 41, make_feats(sections))
    assert [(s.kind, s.start, s.length) for s in secs] == [("intro", 1, 23), ("build", 24, 2), ("drop", 26, 16)]


def test_repeated_build_gets_the_same_labels():
    S = aa.Section
    rng = np.random.default_rng(3)
    bands = rng.normal(0, 8, size=(64, 12))
    bands[40:48] = bands[8:16] + rng.normal(0, 0.5, size=(8, 12))  # bars 41-48 = bars 9-16
    secs = [S(1, 9, "intro"), S(9, 16, "build"), S(16, 17, "predrop"), S(17, 41, "drop"),
            S(41, 49, "breakdown"), S(49, 65, "drop")]
    out = aa._copy_repeated_builds(secs, bands)
    assert [(s.kind, s.start, s.length) for s in out] == [
        ("intro", 1, 8), ("build", 9, 7), ("predrop", 16, 1), ("drop", 17, 24),
        ("build", 41, 7), ("predrop", 48, 1), ("drop", 49, 16)]


def test_repeated_build_not_copied_if_no_drop_follows():
    S = aa.Section
    rng = np.random.default_rng(3)
    bands = rng.normal(0, 8, size=(56, 12))
    bands[40:48] = bands[8:16]  # the copy sits at the very end, in the outro
    secs = [S(1, 9, "intro"), S(9, 16, "build"), S(16, 17, "predrop"), S(17, 41, "drop"),
            S(41, 57, "outro")]
    out = aa._copy_repeated_builds(secs, bands)
    assert [s.kind for s in out] == ["intro", "build", "predrop", "drop", "outro"]


def test_short_section_with_bass_between_drops_is_still_a_breakdown():
    got = classify([(8, -8, -40, -6, -6), (8, -3, -4, -8, -2), (16, 0, 0, 0, 0),
                    (8, -4, -5, -3, -3), (16, 0, 0, 0, 0)])
    assert "groove" not in [g[0] for g in got]


def test_full_level_groove_before_slam_in_drop():
    # Death Car pattern: intro | groove (as loud as the drop!) | 2-bar cut | DROP (bass
    # slams in) | 2-bar cut | DROP 2 | 2-bar cut | groove again | 2-bar cut | DROP 3
    # (like the real track, the bass creeps in a bar early, at bar 16: no slam-in at 17)
    got = classify([(15, -15, -75, -8, -8), (1, -6, -8, -8, -8), (14, 0, 0, -2, -2), (2, -9, -28, -1, -1),
                    (14, -0.5, -1, 2, 2), (2, -5, -30, -2, -2), (14, -0.5, -1, 2, 2),
                    (2, -8, -20, -2, -2), (14, 0, 0, -2, -2), (2, -9, -28, -1, -1), (16, -0.5, -1, 2, 2)])
    # (the groove at 65 comes back after a hard cut, so it LOOKS like a slam-in - it's
    #  recognised because it sounds like the first groove)
    assert [g[0] for g in got] == ["intro", "groove", "build", "drop", "breakdown", "drop",
                                   "breakdown", "groove", "build", "drop"]


def test_stuttering_bass_hits_are_not_arrivals():
    # T11 pattern: a groove of single-bar bass hits with gaps (hit, cut,
    # cut, cut), then a 2-bar cut and the real drop where the bass STAYS in.
    B = [-5] * 4 + [0, -21, -42, -50] * 2 + [-50, -50] + [-1] * 8
    E = [-5] * len(B)
    H = [-3] * len(B)
    arrivals = aa.find_arrivals(feats_from_bars(E, B, H))
    assert arrivals == [15]  # only the drop, not the hits at bars 5 and 9


def test_lead_in_bar_with_gap_on_beat_4_moves_the_drop_to_the_next_bar():
    # T11 pattern: cut | bass in for beats 1-3, gap on beat 4 | drop.
    # The drop lands on the bar AFTER the gap, not where the bass first comes back.
    B = [-5] * 4 + [-40, -40] + [-5] + [-1] * 8
    E, H = [-5] * len(B), [-3] * len(B)
    f = feats_from_bars(E, B, H)
    beats = np.repeat(np.asarray(B, dtype=float)[:, None], 4, axis=1)
    beats[6, 3] = -30  # bar 7: gap on beat 4
    f.bass_beats = beats
    assert aa.find_arrivals(f) == [8]


def test_bouncing_drop_rhythm_is_not_a_lead_in():
    # T3 pattern: the drop's own rhythm has gaps (0 / -12 / -4 / -21 dB). The drop
    # still starts where the bass slams in.
    B = [-5] * 4 + [-40, -40] + [-1] * 8
    E, H = [-5] * len(B), [-3] * len(B)
    f = feats_from_bars(E, B, H)
    beats = np.repeat(np.asarray(B, dtype=float)[:, None], 4, axis=1)
    beats[7] = [0, -12, -4, -21]  # bar 8, the bar after the drop lands
    f.bass_beats = beats
    assert aa.find_arrivals(f) == [7]


def test_part_of_drop_after_a_one_bar_switch_up_cut_is_still_the_drop():
    # T10 pattern: drop 16 | 1-bar bass cut | 7 bars with a part dropped out
    # (a little quieter, bass a few dB down) | build | drop. Still drop 1 up to the build.
    E = [-8] * 8 + [0] * 16 + [-6] + [-2] * 7 + list(np.linspace(-10, -3, 8)) + [0] * 16
    B = [-40] * 8 + [0] * 16 + [-37] + [-4] * 7 + [-30] * 8 + [0] * 16
    H = [-6] * 8 + [0] * 16 + [-3] + [-1] * 7 + list(np.linspace(-8, 0, 8)) + [0] * 16
    secs = aa.classify_sections([1, 9, 25, 26, 33, 41], len(E), feats_from_bars(E, B, H))
    kinds = {b: s.kind for s in secs for b in range(s.start, s.end)}
    assert all(kinds[b] == "drop" for b in range(26, 33))


def test_quiet_bass_carrying_on_from_the_intro_is_still_intro():
    # T12 pattern: bass in from bar 1 but ~10 dB under the drop, carrying on for 16
    # bars, then the bass comes up near drop level (groove), build, drop. Only the
    # near-drop part is the groove.
    sections = [(8, -10, -10, -24, -22), (8, -10, -10, -20, -16), (16, -2, -4, -2, -2),
                (8, -3, -8, -8, 0), (16, 0, 0, 0, 0)]
    got = bar_kinds(classify(sections))
    assert {got[b] for b in range(1, 17)} == {"intro"}
    assert {got[b] for b in range(17, 33)} == {"groove"}


def test_opening_groove_coming_back_straight_after_a_drop_is_a_breakdown():
    # T14 pattern: intro | groove | build | drop | the groove again, bar for bar |
    # build | drop. Straight after the drop it's breaking down into it: a breakdown.
    groove = (8, -2, -2, -9, -6)
    sections = [(8, -12, -35, -10, -8), groove, (8, -9, -37, -6, 1), (32, 0, 0, -2, -2),
                groove, (8, -9, -37, -6, 1), (32, 0, 0, -2, -2)]
    got = bar_kinds(classify(sections))
    assert {got[b] for b in range(9, 17)} == {"groove"}
    assert {got[b] for b in range(57, 65)} == {"breakdown"}


def test_silence_at_the_end_is_the_outro_even_if_short():
    # Ends on a drop, then 2 silent bars (T14): those 2 bars are the outro.
    sections = [(8, -8, -40, -6, -6), (8, -3, -4, -8, -2), (16, 0, 0, 0, 0), (2, -130, -150, -130, -130)]
    got = classify(sections)
    assert got[-1] == ("outro", 33, 2)
    assert ("drop", 17, 16) in got


def test_cant_break_down_a_breakdown():
    # T1 pattern: drop | breakdown (bass gone) | a different, fuller
    # part (beat back, no sub) | build | drop. The second part isn't breaking down from a
    # drop, so it's a groove.
    sections = [(8, -8, -40, -6, -6), (8, -3, -4, -8, -2), (16, 0, 0, 0, 0),
                (12, -12, -50, -15, -15), (24, -7, -17, -3, -3),
                (8, -9, -25, -6, 0), (16, 0, 0, 0, 0)]
    got = bar_kinds(classify(sections))
    assert {got[b] for b in range(33, 45)} == {"breakdown"}
    assert {got[b] for b in range(45, 69)} == {"groove"}


def test_build_straight_out_of_a_drop_is_half_breakdown_half_build():
    # T10 49-56 / T6 65-72 pattern: drop | 8 bars that build into the next
    # drop | drop. Straight out of a drop it breaks down first: 4 + 4.
    sections = [(8, -8, -40, -6, -6), (8, -3, -4, -8, -2), (16, 0, 0, 0, 0),
                (8, -4, -8, -8, 0), (16, 0, 0, 0, 0)]
    got = bar_kinds(classify(sections))
    assert {got[b] for b in range(33, 37)} == {"breakdown"}
    assert {got[b] for b in range(37, 41)} == {"build"}



def test_breakdown_straight_after_a_predrop_is_a_fake_drop():
    # T6 pattern: build | pre-drop | a part that looks like a breakdown | another build
    # | drop. A pre-drop always comes before a drop, so the track faked us out.
    E = [-3] * 8 + [-4] + [1] * 4 + list(np.linspace(-6, -1, 4)) + [0] * 16
    B = [-6] * 8 + [-20] + [2] * 4 + [-6] * 4 + [0] * 16
    H = list(np.linspace(-8, -2, 8)) + [-12] + [-8] * 4 + list(np.linspace(-6, 0, 4)) + [0] * 16
    got = aa.classify_sections([1, 9, 10, 14, 18], len(E), feats_from_bars(E, B, H))
    assert not any(a.kind == "predrop" and b.kind == "breakdown" for a, b in zip(got, got[1:]))


def test_three_bar_predrop_is_redrawn_to_a_whole_phrase_length():
    # Pre-drops are 1, 2 or 4 bars, never 3: pick the length whose first bar is where the
    # sound changes most (T8 110-112 -> 109-112).
    E = [-2] * 16 + [-1] + [-10] * 3 + [0] * 16
    B = [-2] * 16 + [0.3] + [-70] * 3 + [0] * 16
    H = [-1] * 16 + [-8] + [-1] * 3 + [0] * 16  # bar 17 already sounds different
    got = aa.classify_sections([1, 17, 21], len(E), feats_from_bars(E, B, H))
    pre = [s for s in got if s.kind == "predrop"]
    assert pre and all(s.length in (1, 2, 4) for s in pre)


def test_stripped_build_halves_and_groove_after_breakdown():
    # T14 pattern: intro | groove | 8 stripped bars, flat then brighter (half
    # breakdown, half build) | drop | the groove again (breakdown) | the stripped part
    # again: can't break down a breakdown, so a 4-bar groove | build | drop.
    E = [-10] * 8 + [-2] * 8 + [-10] * 8 + [0] * 32 + [-2] * 8 + [-10] * 4 + [-8] * 4 + [0] * 16
    B = [-37] * 8 + [-2] * 8 + [-37] * 8 + [0] * 32 + [-2] * 8 + [-37] * 4 + [-37] * 4 + [0] * 16
    H = ([-6] * 8 + [-5] * 8 + [-6] * 4 + [-1] * 4 + [-2] * 32 + [-5] * 8
         + [-6] * 4 + [-1] * 4 + [-2] * 16)
    secs = aa.classify_sections([1, 9, 17, 25, 41, 57, 65, 69, 73], len(E), feats_from_bars(E, B, H))
    kinds = {b: s.kind for s in secs for b in range(s.start, s.end)}
    assert {kinds[b] for b in range(17, 21)} == {"breakdown"}
    assert {kinds[b] for b in range(21, 25)} == {"build"}
    assert {kinds[b] for b in range(57, 65)} == {"breakdown"}
    assert {kinds[b] for b in range(65, 69)} == {"groove"}
    assert {kinds[b] for b in range(69, 73)} == {"build"}


def test_breakdown_starts_on_the_drops_8_bar_grid():
    # T13 pattern: drop lands on 17; the bass cuts out at 79-80 as an "end of the
    # drop", breakdown from 81. The phrase grid put the change at 77 (4 bars off the
    # drop's 8-bar phrases, and bar 77 is still full): it belongs at 81.
    E = [-8] * 8 + [-5] * 8 + [0] * 61 + [-7, -8, -7] + [-12] * 8 + [-6] * 8 + [0] * 16
    B = [-40] * 8 + [-35] * 8 + [0] * 61 + [-10, -40, -55] + [-25] * 4 + [-50] * 4 + [-12] * 8 + [0] * 16
    H = [-6] * 8 + list(np.linspace(-8, 0, 8)) + [-1] * 61 + [-1, -1, -4] + [-10] * 8 + list(np.linspace(-8, 0, 8)) + [-1] * 16
    secs = aa.classify_sections([1, 9, 17, 33, 49, 65, 77, 85, 89, 97], len(E), feats_from_bars(E, B, H))
    kinds = {b: s.kind for s in secs for b in range(s.start, s.end)}
    assert {kinds[b] for b in range(17, 81)} == {"drop"}
    assert kinds[81] == "breakdown"




def test_part_after_a_build_is_never_the_outro():
    # T12 pattern: last drop | build (bass dips then cuts, highs up) | bass back
    # near full for 16 bars (breakdown) | fading out (outro). A build always leads into
    # something, so the part straight after it is a breakdown, and the outro comes after.
    E = ([-8] * 8 + list(np.linspace(-8, -3, 8)) + [0] * 16 + [-3, -4, -4, -4]
         + [-2] * 16 + list(np.linspace(-9, -18, 8)))
    B = ([-40] * 8 + [-30] * 8 + [0] * 16 + [-4, -6, -23, -16]
         + [-2] * 16 + list(np.linspace(-11, -50, 8)))
    H = ([-6] * 8 + list(np.linspace(-8, 0, 8)) + [-1] * 16 + [0, -1, 1, 0]
         + [0] * 16 + list(np.linspace(-9, -16, 8)))
    secs = aa.classify_sections([1, 9, 17, 33, 37, 53], len(E), feats_from_bars(E, B, H))
    kinds = {b: s.kind for s in secs for b in range(s.start, s.end)}
    assert {kinds[b] for b in range(17, 33)} == {"drop"}
    assert {kinds[b] for b in range(33, 37)} == {"build"}
    assert {kinds[b] for b in range(37, 53)} == {"breakdown"}
    assert {kinds[b] for b in range(53, 61)} == {"outro"}
