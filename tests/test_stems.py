"""Tests for background stem analysis (djplanner/stems.py), with a fake separator (no Demucs)."""

import time

import numpy as np

from djplanner import stems
from djplanner.beatgrid import Beatgrid


class FakeSeparator:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def levels(self, path, grid):
        self.calls += 1
        if self.fail:
            raise RuntimeError("bad file")
        return {name: [-10.0] * grid.bar_count for name in stems.STEM_NAMES}


def wait_until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def make(tmp_path, sep):
    a = stems.StemAnalyser(tmp_path / "stems", separator=sep)
    a.enabled = True  # (don't depend on PyTorch being installed for these tests)
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fake audio")
    return a, audio, Beatgrid.constant(174, bars=16)


def test_first_ask_queues_then_result_is_ready_and_saved(tmp_path):
    sep = FakeSeparator()
    a, audio, grid = make(tmp_path, sep)
    assert a.get("t1", audio, grid) is None  # not ready yet: queued
    assert a.status("t1") in ("queued", "working", "ready")
    assert wait_until(lambda: a.status("t1") == "ready")
    levels = a.get("t1", audio, grid)
    assert set(levels) == set(stems.STEM_NAMES) and len(levels["vocals"]) == 16
    assert (tmp_path / "stems" / "t1.json").exists()
    # a fresh analyser (e.g. after a restart) reuses the saved result: no new separation
    b = stems.StemAnalyser(tmp_path / "stems", separator=sep)
    assert b.get("t1", audio, grid) == levels and sep.calls == 1


def test_changed_beatgrid_means_redo(tmp_path):
    sep = FakeSeparator()
    a, audio, grid = make(tmp_path, sep)
    a.get("t1", audio, grid)
    assert wait_until(lambda: a.status("t1") == "ready")
    b = stems.StemAnalyser(tmp_path / "stems", separator=sep)
    b.enabled = True
    assert b.get("t1", audio, Beatgrid.constant(170, bars=16)) is None  # different grid: queued again
    assert wait_until(lambda: sep.calls == 2)


def test_failure_is_reported_not_retried_forever(tmp_path):
    sep = FakeSeparator(fail=True)
    a, audio, grid = make(tmp_path, sep)
    a.get("t1", audio, grid)
    assert wait_until(lambda: a.status("t1").startswith("failed"))
    a.get("t1", audio, grid)
    time.sleep(0.1)
    assert sep.calls == 1


def test_unavailable_without_demucs(tmp_path):
    a, audio, grid = make(tmp_path, FakeSeparator())
    a.enabled = False
    assert a.get("t1", audio, grid) is None
    assert a.status("t1") == "unavailable"


def test_per_bar_db():
    grid = Beatgrid.constant(120, bars=2)  # 2 s per bar at 120 BPM
    sr = 1000
    x = np.concatenate([np.full(2 * sr, 0.1), np.full(2 * sr, 0.01)]).astype(np.float32)
    db = stems.per_bar_db(x, grid, sr)
    assert db[0] == -20.0 and db[1] == -40.0
