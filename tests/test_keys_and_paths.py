"""Tests for Camelot key conversion and audio-file path remapping."""

from pathlib import Path

import pytest

from djplanner.keys import to_camelot
from djplanner.rekordbox_reader import resolve_file_path


@pytest.mark.parametrize(
    "key, camelot",
    [
        ("Abm", "1A"), ("G#m", "1A"), ("Ebm", "2A"), ("Fm", "4A"), ("Cm", "5A"),
        ("Em", "9A"), ("Dbm", "12A"), ("E", "12B"), ("Eb", "5B"), ("F#", "2B"),
        ("C", "8B"), ("8A", "8A"), ("12b", "12B"), ("A minor", "8A"), ("", ""),
        (None, ""), ("H#", ""),
    ],
)
def test_to_camelot(key, camelot):
    assert to_camelot(key) == camelot


REMAPS = {"D:/Documents/Music": "C:/Users/me/Documents/Music"}


def fake_fs(*existing):
    """An `exists` function that only knows about the given paths."""
    return lambda p: p in existing


def test_path_used_as_is_when_it_exists():
    path, ok = resolve_file_path("C:/music/a.mp3", REMAPS, fake_fs("C:/music/a.mp3"))
    assert (path, ok) == ("C:/music/a.mp3", True)


def test_remap_applies_case_insensitively_and_with_backslashes():
    target = "C:/Users/me/Documents/Music/sub/track.wav"
    path, ok = resolve_file_path(r"d:\documents\music\sub\track.wav", REMAPS, fake_fs(target))
    assert (path, ok) == (target, True)


def test_remap_does_not_match_partial_folder_names():
    path, ok = resolve_file_path(
        "D:/Documents/MusicOld/x.wav", REMAPS, fake_fs("C:/Users/me/Documents/MusicOld/x.wav")
    )
    assert ok is False


def test_missing_everywhere():
    assert resolve_file_path("D:/Documents/Music/x.wav", REMAPS, fake_fs()) == (
        "D:/Documents/Music/x.wav",
        False,
    )


def test_snapshot_gets_a_new_name_and_old_copies_are_tidied(tmp_path):
    # Each load copies master.db to a fresh file, so a copy still held open by an
    # earlier connection never has to be overwritten (Windows refuses: WinError 1224).
    from djplanner.rekordbox_reader import RekordboxLibrary
    rb = tmp_path / "rekordbox"
    rb.mkdir()
    (rb / "master.db").write_bytes(b"db")
    (rb / "master.db-shm").write_bytes(b"shm")
    lib = RekordboxLibrary(rekordbox_dir=rb, cache_dir=tmp_path / "cache")
    first = lib._snapshot_db()
    second = lib._snapshot_db()
    assert first != second and second.read_bytes() == b"db"
    assert Path(str(second) + "-shm").read_bytes() == b"shm"
    # the earlier copy and its side file are gone; the originals are untouched
    assert sorted(f.name for f in (tmp_path / "cache").iterdir()) == [second.name, second.name + "-shm"]
    assert (rb / "master.db").read_bytes() == b"db"


def test_load_path_remaps(tmp_path):
    from djplanner.config import load_path_remaps

    # no file = no remaps
    assert load_path_remaps(tmp_path / "missing.json") == {}
    f = tmp_path / "path_remaps.json"
    f.write_text('{"D:/Old/Music": "C:/New/Music"}', encoding="utf-8")
    assert load_path_remaps(f) == {"D:/Old/Music": "C:/New/Music"}
    # anything other than folder -> folder strings is a clear error
    f.write_text('["D:/Old/Music"]', encoding="utf-8")
    with pytest.raises(ValueError):
        load_path_remaps(f)
