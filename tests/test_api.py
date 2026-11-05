"""Tests for the web API, using a fake in-memory library (no Rekordbox needed)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from djplanner import api
from djplanner.analysis import fallback_phrases
from djplanner.beatgrid import Beatgrid
from djplanner.models import Track, TrackSummary


def fake_track(tid, title, has_grid=True):
    grid = Beatgrid.constant(174, bars=32) if has_grid else Beatgrid([], [])
    summary = TrackSummary(
        id=tid, title=title, artist="Tester", bpm=174.0 if has_grid else 0, key="Fm",
        camelot="4A", duration_s=44.1, file_path="x.wav", original_path="x.wav",
        file_exists=True, has_beatgrid=has_grid, has_phrases=False,
    )
    return Track(summary=summary, beatgrid=grid, phrases=fallback_phrases(grid, 16), cues=[])


class FakeLibrary:
    rekordbox_dir = "fake"
    loaded_at = datetime(2026, 1, 1, 12, 0)
    playlists = []

    def __init__(self):
        self.tracks = {"1": fake_track("1", "Roller"), "2": fake_track("2", "One-shot", False)}

    def load(self):
        pass

    def track_ids(self):
        return list(self.tracks)

    def list_tracks(self, include_unanalysed=False):
        return [t.summary for t in self.tracks.values() if include_unanalysed or t.summary.has_beatgrid]

    def get_track(self, tid):
        return self.tracks[tid]


@pytest.fixture
def client():
    api.set_library(FakeLibrary())
    with TestClient(api.app) as c:
        yield c


def test_library_status(client):
    r = client.get("/api/library").json()
    assert r["loaded"] is True and r["track_count"] == 2


def test_track_list_hides_tracks_without_grid_by_default(client):
    assert [t["title"] for t in client.get("/api/tracks").json()] == ["Roller"]
    assert len(client.get("/api/tracks?all=true").json()) == 2


def test_track_detail(client):
    t = client.get("/api/tracks/1").json()
    assert t["bar_count"] == 32
    assert [p["label"] for p in t["phrases"]] == ["Bars 1-16", "Bars 17-32"]
    assert len(t["bar_times"]) == 33  # start of bars 1..33
    assert t["phrases"][1]["end_bar"] == 33.0


def test_unknown_track_is_404(client):
    assert client.get("/api/tracks/nope").status_code == 404


def test_audio_streams_with_range(client, tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(bytes(range(256)) * 4)  # 1 KB of fake "audio"
    lib = api._library
    lib.tracks["1"].summary.file_path = str(audio)
    full = client.get("/api/tracks/1/audio")
    assert full.status_code == 200 and full.headers["content-type"] == "audio/mpeg"
    assert len(full.content) == 1024
    part = client.get("/api/tracks/1/audio", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == (bytes(range(256)) * 4)[100:200]


def test_audio_missing_file_is_404(client):
    api._library.tracks["1"].summary.file_exists = False
    assert client.get("/api/tracks/1/audio").status_code == 404
    assert client.get("/api/tracks/nope/audio").status_code == 404


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "EZMix" in r.text  # (the user renamed the app, 2026-10-15)


def test_page_files_are_never_served_stale(client):
    # the browser must re-check app.js etc. on every refresh, so changes show up
    r = client.get("/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_settings_round_trip(client, tmp_path, monkeypatch):
    from djplanner import user_settings
    monkeypatch.setattr(user_settings, "SETTINGS_FILE", tmp_path / "user_settings.json")
    # save() / load() take the path as a default argument, so point them at it too
    monkeypatch.setattr(user_settings.save, "__defaults__", (tmp_path / "user_settings.json",))
    monkeypatch.setattr(user_settings.load, "__defaults__", (tmp_path / "user_settings.json",))
    assert client.get("/api/settings").json() == {"phrase_source": "detected"}
    assert client.put("/api/settings", json={"phrase_source": "rekordbox"}).json()["phrase_source"] == "rekordbox"
    assert client.get("/api/settings").json()["phrase_source"] == "rekordbox"
    assert client.put("/api/settings", json={"phrase_source": "custom"}).json()["phrase_source"] == "custom"
    assert client.put("/api/settings", json={"phrase_source": "none"}).status_code == 400  # renamed
    assert client.put("/api/settings", json={"phrase_source": "nonsense"}).status_code == 400
    assert client.put("/api/settings", json={"colour": "red"}).status_code == 400


def test_old_none_setting_becomes_custom(tmp_path):
    from djplanner import user_settings
    f = tmp_path / "user_settings.json"
    f.write_text('{"phrase_source": "none"}', encoding="utf-8")
    assert user_settings.load(f)["phrase_source"] == "custom"


# ----- custom sections (phrase editor) ---------------------------------------------------


@pytest.fixture
def custom_client(tmp_path):
    lib = FakeLibrary()
    lib.custom_file = tmp_path / "custom_phrases.json"
    from djplanner.models import Phrase
    lib.detected_phrases = lambda tid: [
        Phrase("intro", "Intro", 1.0, 8.0, "detected"), Phrase("build", "Build", 9.0, 8.0, "detected"),
        Phrase("drop", "Drop", 17.0, 16.0, "detected")]
    api.set_library(lib)
    with TestClient(api.app) as c:
        yield c, lib


def test_custom_sections_start_blank(custom_client):
    client, _ = custom_client
    r = client.get("/api/tracks/1/custom").json()
    assert r["exists"] is False and r["sections"] == []
    assert client.get("/api/tracks/nope/custom").status_code == 404


def test_custom_sections_save_and_load(custom_client):
    client, lib = custom_client
    body = {"sections": [["Drop", 17, 32], ["intro", 1, 16]], "note": "bass in at 17", "based_on": "blank"}
    r = client.put("/api/tracks/1/custom", json=body).json()
    assert r["sections"] == [["Intro", 1, 16], ["Drop", 17, 32]]  # sorted, standard names
    again = client.get("/api/tracks/1/custom").json()
    assert again["exists"] and again["note"] == "bass in at 17" and again["title"] == "Roller"
    assert lib.custom_file.exists()


def test_custom_sections_are_checked(custom_client):
    client, _ = custom_client
    put = lambda secs: client.put("/api/tracks/1/custom", json={"sections": secs})
    assert put([["Intro", 1, 16], ["Drop", 16, 32]]).status_code == 400  # overlap
    assert put([["Intro", 9, 4]]).status_code == 400  # backwards
    assert put([["Chorus", 1, 4]]).status_code == 400  # not one of our kinds
    assert put([["Intro", 1, 33]]).status_code == 400  # past the end (32 bars)
    assert put([["Intro", 0, 4]]).status_code == 400  # before bar 1
    assert put([["Intro", 1, 4.5]]).status_code == 400  # whole bars only
    assert put([["Intro", 1, 8], ["Drop", 17, 32]]).status_code == 200  # gaps are fine


def test_custom_from_detected_copies_the_auto_sections(custom_client):
    client, _ = custom_client
    r = client.post("/api/tracks/1/custom/from-detected").json()
    assert r["based_on"] == "detected"
    assert r["sections"] == [["Intro", 1, 8], ["Build", 9, 16], ["Drop", 17, 32]]


def test_custom_delete_goes_back_to_blank(custom_client):
    client, _ = custom_client
    client.put("/api/tracks/1/custom", json={"sections": [["Intro", 1, 8]]})
    assert client.delete("/api/tracks/1/custom").json()["exists"] is False
    assert client.get("/api/tracks/1/custom").json()["sections"] == []


def test_custom_from_detected_without_saving(custom_client):
    client, _ = custom_client
    r = client.post("/api/tracks/1/custom/from-detected?save=false").json()
    assert len(r["sections"]) == 3
    assert client.get("/api/tracks/1/custom").json()["exists"] is False  # nothing saved
