"""Tests for the WAV copies used by in-browser previews (djplanner/preview_audio.py)."""

import subprocess
import wave

import pytest

from djplanner import preview_audio
from djplanner.audio_analysis import find_ffmpeg


def test_non_mp3_is_served_as_it_is(tmp_path):
    f = tmp_path / "track.wav"
    f.write_bytes(b"RIFF")
    assert preview_audio.playable_path(f) == f


@pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg not installed")
def test_mp3_gets_a_cached_wav_copy_and_original_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_audio, "AUDIO_CACHE_DIR", tmp_path / "cache")
    mp3 = tmp_path / "song.mp3"
    # 2 seconds of a test tone, VBR-encoded like the problem tracks
    subprocess.run([find_ffmpeg(), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-q:a", "2", str(mp3)], check=True)
    before = mp3.read_bytes()

    out = preview_audio.playable_path(mp3)
    assert out.suffix == ".wav" and out.parent == tmp_path / "cache"
    with wave.open(str(out)) as w:
        assert abs(w.getnframes() / w.getframerate() - 2.0) < 0.05
    assert mp3.read_bytes() == before  # the original is only read
    assert preview_audio.playable_path(mp3) == out  # second time: same cached copy
    assert not list((tmp_path / "cache").glob("*.part.wav"))  # no leftovers
