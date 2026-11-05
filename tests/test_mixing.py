"""Tests for Phase 4 mixing (djplanner/mixing.py): finding mixes and the checks."""

from djplanner import mixing as mx
from djplanner.analysis import fallback_phrases
from djplanner.beatgrid import Beatgrid
from djplanner.models import Phrase, Plan, Segment, Track, TrackSummary


def seg(id, track, deck, set_bar, length, start_bar=1.0, kind="intro", label="", bpm=174.0):
    return Segment(id=id, track_id=track, start_bar=start_bar, length_bars=length,
                   label=label or kind.title(), deck=deck, set_bar=set_bar, kind=kind,
                   track_title=f"Track {track}", bpm=bpm)


def plan(*segs):
    return Plan.from_dict({"id": "p", "name": "t", "segments": [s.to_dict() for s in segs]})


def track(tid, bpm=174.0, key="Fm", camelot="4A", phrases=None, bars=64):
    grid = Beatgrid.constant(bpm, bars=bars)
    summ = TrackSummary(id=tid, title=f"Track {tid}", artist="", bpm=bpm, key=key, camelot=camelot,
                        duration_s=bars * 4 * 60 / bpm, file_path="x.wav", original_path="x.wav",
                        file_exists=True, has_beatgrid=True, has_phrases=True)
    return Track(summary=summ, beatgrid=grid, phrases=phrases or fallback_phrases(grid, 16), cues=[])


def lib(*tracks):
    d = {t.id: t for t in tracks}
    return lambda tid: d[tid]


# ----- finding mixes -------------------------------------------------------------------

def test_overlap_between_decks_is_one_mix():
    p = plan(seg("a", "A", 1, 1, 32, kind="drop"), seg("b", "B", 2, 25, 32))
    [m] = mx.find_mixes(p)
    assert (m.out_seg.id, m.in_seg.id, m.start, m.end, m.bars, m.layer) == ("a", "b", 25, 33, 8, False)
    assert m.out_track_bar(25) == 25 and m.in_track_bar(25) == 1


def test_back_to_back_blocks_of_one_track_make_one_mix_not_two():
    # Deck 1 plays Drop then Outro (same track, no gap); Deck 2 comes in across both
    p = plan(seg("a1", "A", 1, 1, 16, kind="drop"), seg("a2", "A", 1, 17, 16, start_bar=49, kind="outro"),
             seg("b", "B", 2, 13, 32))
    [m] = mx.find_mixes(p)
    assert (m.start, m.end) == (13, 33)
    assert m.out_track_bar(13) == 13 and m.out_track_bar(20) == 52  # jumped to the outro
    assert m.out_seg.id == "a1"


def test_short_block_inside_a_long_one_is_a_layer():
    p = plan(seg("a", "A", 1, 1, 64, kind="drop"), seg("b", "B", 2, 17, 8))
    [m] = mx.find_mixes(p)
    assert m.layer


def test_same_deck_never_mixes():
    assert mx.find_mixes(plan(seg("a", "A", 1, 1, 16), seg("b", "B", 1, 33, 16))) == []


# ----- checks --------------------------------------------------------------------------

def test_tempo_gap_treats_half_time_as_the_same():
    assert mx.tempo_gap(174, 174) == 0
    assert round(mx.tempo_gap(174, 87), 3) == 0
    assert round(mx.tempo_gap(140, 174), 2) == 0.24
    assert mx.tempo_gap(0, 174) is None


def test_camelot_neighbours():
    assert mx.camelot_compatible("4A", "4A")
    assert mx.camelot_compatible("4A", "5A") and mx.camelot_compatible("4A", "3A")
    assert mx.camelot_compatible("12A", "1A")  # wraps round
    assert mx.camelot_compatible("4A", "4B")  # relative major / minor
    assert not mx.camelot_compatible("4A", "6A")
    assert not mx.camelot_compatible("4A", "5B")
    assert mx.camelot_compatible("", "4A") is None


def test_checks_flag_tempo_and_key():
    p = plan(seg("a", "A", 1, 1, 48, kind="drop"), seg("b", "B", 2, 33, 32))
    [m] = mx.find_mixes(p)
    get = lib(track("A", 140, "Fm", "4A"), track("B", 174, "Am", "8A"))
    msgs = mx.check_mix(m, get)
    assert any(w["message"].startswith("Tempo gap") for w in msgs)
    assert any(w["message"].startswith("Key clash") for w in msgs)
    assert all(w["segment_id"] == "b" for w in msgs)


def test_vocal_clash_needs_vocals_on_both():
    p = plan(seg("a", "A", 1, 1, 48, kind="drop"), seg("b", "B", 2, 33, 32))
    [m] = mx.find_mixes(p)
    get = lib(track("A"), track("B"))
    vocals = {"A": {"vocals": [0.0] * 64}, "B": {"vocals": [0.0] * 64}}
    msgs = mx.check_mix(m, get, lambda t: vocals[t])
    assert any(w["message"].startswith("Vocal clash") for w in msgs)
    vocals["B"] = {"vocals": [-40.0] * 64, "drums": [0.0] * 64}  # instrumental
    assert not any(w["message"].startswith("Vocal clash") for w in mx.check_mix(m, get, lambda t: vocals[t]))
    # stems not ready yet: no vocal check, no crash
    assert not any(w["message"].startswith("Vocal clash") for w in mx.check_mix(m, get, lambda t: None))


def test_off_phrase_mix_is_mentioned():
    phrases = [Phrase("intro", "Intro", 1.0, 16.0), Phrase("drop", "Drop", 17.0, 32.0)]
    get = lib(track("A", phrases=phrases), track("B"))
    [m] = mx.find_mixes(plan(seg("a", "A", 1, 1, 48, kind="drop"), seg("b", "B", 2, 37, 12)))
    assert any("into a phrase" in w["message"] for w in mx.check_mix(m, get))  # 20 bars into the drop
    [m] = mx.find_mixes(plan(seg("a", "A", 1, 1, 48, kind="drop"), seg("b", "B", 2, 41, 8)))
    assert not any("into a phrase" in w["message"] for w in mx.check_mix(m, get))  # 24 = 3 x 8
