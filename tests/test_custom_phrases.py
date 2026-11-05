"""Tests for custom (hand-edited) phrase sections: djplanner/custom_phrases.py."""

import pytest

from djplanner import custom_phrases as cp
from djplanner.models import Phrase


def test_numbered_labels_like_the_detector():
    kinds = ["intro", "build", "drop", "drop", "breakdown", "build", "drop", "outro", "outro"]
    assert cp.numbered_labels(kinds) == [
        "Intro", "Build 1", "Drop 1", "Drop 1", "Breakdown", "Build 2", "Drop 2", "Outro", "Outro"]


def test_to_phrases_and_back():
    secs = [["Intro", 1, 16], ["Pre-drop", 17, 18], ["Drop", 19, 50]]
    phrases = cp.to_phrases(secs)
    assert [(p.kind, p.label, p.start_bar, p.length_bars, p.source) for p in phrases] == [
        ("intro", "Intro", 1.0, 16.0, "custom"), ("predrop", "Pre-drop", 17.0, 2.0, "custom"),
        ("drop", "Drop", 19.0, 32.0, "custom")]
    assert cp.from_phrases(phrases) == secs


def test_from_phrases_skips_rekordbox_kinds():
    assert cp.from_phrases([Phrase("chorus", "Chorus", 1.0, 16.0)]) == []


def test_validate_accepts_any_spelling_of_names():
    assert cp.validate([["fake drop", 5, 8], ["PREDROP", 1, 4]]) == [["Pre-drop", 1, 4], ["Fake drop", 5, 8]]
    with pytest.raises(ValueError, match="overlap"):
        cp.validate([["Intro", 1, 8], ["Drop", 8, 9]])


def test_save_load_delete(tmp_path):
    f = tmp_path / "c.json"
    assert cp.get("7", f) is None
    cp.save("7", "Song", [["Intro", 1, 8]], based_on="detected", note="hi", path=f)
    e = cp.get("7", f)
    assert e["sections"] == [["Intro", 1, 8]] and e["based_on"] == "detected" and e["title"] == "Song"
    assert cp.delete("7", f) is True and cp.get("7", f) is None
    assert cp.delete("7", f) is False


def test_bad_file_means_no_custom_sections(tmp_path):
    f = tmp_path / "c.json"
    f.write_text("not json", encoding="utf-8")
    assert cp.load_all(f) == {}


def test_same_kind_with_a_gap_between_is_two_sections():
    phrases = cp.to_phrases([["Drop", 1, 8], ["Drop", 17, 24], ["Drop", 25, 32]])
    assert [p.label for p in phrases] == ["Drop 1", "Drop 2", "Drop 2"]
