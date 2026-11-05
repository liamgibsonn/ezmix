"""Tests for turning raw Rekordbox analysis into phrases, cues and waveforms."""

import pytest

from djplanner.analysis import (
    MOOD_HIGH,
    MOOD_MID,
    build_beatgrid,
    build_cue,
    fallback_phrases,
    parse_phrases,
    waveform_per_beat,
)
from djplanner.beatgrid import Beatgrid


def entry(beat, kind, fill=0, beat_fill=0):
    """One fake PSSI phrase entry (beat numbers are 1-based like Rekordbox's)."""
    return {"beat": beat, "kind": kind, "fill": fill, "beat_fill": beat_fill}


def test_build_beatgrid_from_pqtz_entries():
    g = build_beatgrid([{"time": 100, "beat": 1}, {"time": 600, "beat": 2}])
    assert g.times == [0.1, 0.6] and g.numbers == [1, 2]


def test_high_mood_phrases_match_real_track_layout():
    # Mirrors "T5": Intro 8 bars, Up 8 bars, Chorus 16, Outro.
    g = Beatgrid.constant(140, bars=40)
    ph = parse_phrases(
        MOOD_HIGH,
        [entry(1, 1), entry(33, 2), entry(65, 5, fill=1, beat_fill=125), entry(129, 6)],
        end_beat=161,
        grid=g,
    )
    assert [(p.label, p.kind, p.start_bar, p.length_bars) for p in ph] == [
        ("Intro", "intro", 1.0, 8.0),
        ("Up", "up", 9.0, 8.0),
        ("Chorus", "chorus", 17.0, 16.0),
        ("Outro", "outro", 33.0, 8.0),
    ]
    assert ph[2].fill_start_bar == 32.0  # beat 125 -> bar 32
    assert ph[2].end_bar == 33.0


def test_repeated_labels_get_numbered():
    g = Beatgrid.constant(174, bars=40)
    ph = parse_phrases(
        MOOD_HIGH, [entry(1, 5), entry(33, 3), entry(65, 5), entry(97, 5)], end_beat=129, grid=g
    )
    assert [p.label for p in ph] == ["Chorus 1", "Down", "Chorus 2", "Chorus 3"]


def test_mid_mood_uses_verse_table_and_letters_for_numbered_repeats():
    g = Beatgrid.constant(120, bars=20)
    ph = parse_phrases(MOOD_MID, [entry(1, 2), entry(17, 9), entry(33, 2)], end_beat=49, grid=g)
    assert [p.label for p in ph] == ["Verse 1a", "Chorus", "Verse 1b"]


def test_phrases_entries_out_of_order_and_unknown_kind():
    g = Beatgrid.constant(120, bars=20)
    ph = parse_phrases(MOOD_HIGH, [entry(33, 99), entry(1, 1)], end_beat=65, grid=g)
    assert [p.label for p in ph] == ["Intro", "Phrase 99"]
    assert ph[1].kind == "unknown"


def test_phrases_with_pickup_beats_land_on_bars():
    # Grid has 1 pickup beat, so phrase at beat number 2 (index 1) is bar 1.
    g = Beatgrid.constant(120, bars=8, pickup_beats=1)
    ph = parse_phrases(MOOD_HIGH, [entry(2, 1), entry(18, 5)], end_beat=34, grid=g)
    assert [(p.start_bar, p.length_bars) for p in ph] == [(1.0, 4.0), (5.0, 4.0)]


def test_fallback_blocks_cover_whole_track():
    g = Beatgrid.constant(128, bars=40)
    ph = fallback_phrases(g, 16)
    assert [(p.label, p.start_bar, p.length_bars) for p in ph] == [
        ("Bars 1-16", 1.0, 16.0),
        ("Bars 17-32", 17.0, 16.0),
        ("Bars 33-40", 33.0, 8.0),
    ]
    assert all(p.kind == "bars" and p.source == "auto" for p in ph)


def test_fallback_on_empty_grid_is_empty():
    assert fallback_phrases(Beatgrid([], []), 16) == []


def test_hot_cue_memory_cue_and_loop():
    g = Beatgrid.constant(120, bars=32)  # 2 s per bar
    hot = build_cue(1, 64000, -1, None, -1, g)
    assert (hot.type, hot.pad, hot.bar, hot.loop_end_s, hot.color) == ("hot", "A", 33.0, None, None)

    mem = build_cue(0, 2000, None, "drop", 3, g)
    assert (mem.type, mem.pad, mem.bar, mem.comment, mem.color) == ("memory", None, 2.0, "drop", 3)

    loop = build_cue(2, 4000, 6000, "", 255, g)
    assert loop.pad == "B" and loop.loop_end_bar == pytest.approx(4.0)


def test_unknown_hot_cue_kind_has_no_pad():
    c = build_cue(42, 1000, -1, None, None, Beatgrid.constant(120, bars=4))
    assert c.type == "hot" and c.pad is None


def test_cue_on_track_without_grid():
    c = build_cue(1, 1000, -1, None, None, Beatgrid([], []))
    assert c.bar == 0.0


def test_waveform_is_peak_per_beat():
    g = Beatgrid.constant(60, bars=1)  # 1 beat per second -> 150 columns per beat
    heights = [0] * 150 + [31] * 150 + [10] * 150 + [5] * 150
    heights[50] = 20  # a peak inside beat 0
    assert waveform_per_beat(heights, g) == pytest.approx([20 / 31, 1.0, 10 / 31, 5 / 31])


def test_waveform_shorter_than_grid_pads_with_zero():
    g = Beatgrid.constant(60, bars=1)
    assert waveform_per_beat([31] * 150, g) == pytest.approx([1.0, 0.0, 0.0, 0.0])
