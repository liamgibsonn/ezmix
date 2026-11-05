"""Tests for bar <-> seconds conversion, using perfectly steady fake grids."""

import pytest

from djplanner.beatgrid import Beatgrid

# 120 BPM = 0.5 s per beat = 2 s per bar. Easy numbers to check by hand.
BPM = 120


def test_bar_1_starts_at_first_downbeat():
    g = Beatgrid.constant(BPM, bars=8, offset_s=0.1)
    assert g.bar_to_time(1) == pytest.approx(0.1)
    assert g.bar_to_time(2) == pytest.approx(2.1)
    assert g.bar_count == 8


def test_time_to_bar_round_trip():
    g = Beatgrid.constant(BPM, bars=32, offset_s=0.25)
    for bar in (1, 1.25, 7.5, 16.75, 32):
        assert g.time_to_bar(g.bar_to_time(bar)) == pytest.approx(bar)


def test_pickup_beats_come_before_bar_1():
    # 2 beats before the first downbeat: grid starts at bar 0.5.
    g = Beatgrid.constant(BPM, bars=4, pickup_beats=2)
    assert g.first_downbeat_index == 2
    assert g.beat_index_to_bar(0) == pytest.approx(0.5)
    assert g.bar_to_time(1) == pytest.approx(1.0)  # 2 beats x 0.5 s
    assert g.bar_count == 4


def test_extrapolates_past_the_end_at_last_tempo():
    g = Beatgrid.constant(BPM, bars=4)
    # grid has 16 beats (0..7.5 s); bar 6 would start at 10 s
    assert g.bar_to_time(6) == pytest.approx(10.0)
    assert g.time_to_bar(10.0) == pytest.approx(6.0)


def test_tempo_change_mid_track():
    # 4 beats at 120 BPM (0.5 s) then 4 beats at 60 BPM (1 s)
    times = [0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
    g = Beatgrid(times, [1, 2, 3, 4, 1, 2, 3, 4])
    assert g.bar_to_time(2) == pytest.approx(2.0)
    assert g.bar_to_time(2.5) == pytest.approx(4.0)
    assert g.time_to_bar(3.5) == pytest.approx(2.375)
    assert g.bpm_at_bar(1) == pytest.approx(120)
    assert g.bpm_at_bar(2) == pytest.approx(60)


@pytest.mark.parametrize(
    "position, expected",
    [(1.0, (1, 1)), (32.75, (32, 4)), (33.0, (33, 1)), (16.5, (16, 3)), (8.99, (9, 1))],
)
def test_bar_and_beat_labels(position, expected):
    assert Beatgrid.bar_and_beat(position) == expected


def test_empty_grid():
    g = Beatgrid([], [])
    assert len(g) == 0 and g.bar_count == 0
    with pytest.raises(ValueError):
        g.bar_to_time(1)


def test_mismatched_lists_rejected():
    with pytest.raises(ValueError):
        Beatgrid([0, 1], [1])
