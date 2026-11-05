"""Tests for plan models, saving/loading, plan checks and the plan API."""

import json

import pytest
from fastapi.testclient import TestClient

from djplanner import api
from djplanner.models import Plan, Segment
from djplanner.plans import PlanStore, check_plan
from test_api import FakeLibrary


def seg(sid="s1", track_id="1", start=1, length=16, label="Bars 1-16", deck=1, at=1, title=None):
    """A fake segment: `start` = track bar, `at` = set bar."""
    return {"id": sid, "track_id": track_id, "start_bar": start, "length_bars": length,
            "label": label, "deck": deck, "set_bar": at, "track_title": title or f"T{track_id}"}


def messages(plan_dict, library=None):
    """Run the checks; returns {segment_id: [(level, message), ...]}."""
    out = {}
    for w in check_plan(Plan.from_dict(plan_dict), library):
        out.setdefault(w["segment_id"], []).append((w["level"], w["message"]))
    return out


# ----- models ---------------------------------------------------------------------------


def test_plan_round_trip_through_dict():
    p = Plan.from_dict({"name": "Friday", "segments": [seg(), seg("s2", start=17, at=17)],
                        "settings": {"decks": 4, "instant_doubles": True}})
    again = Plan.from_dict(json.loads(json.dumps(p.to_dict())))
    assert again.name == "Friday"
    assert [s.start_bar for s in again.segments] == [1, 17]
    assert again.settings.decks == 4 and again.settings.instant_doubles
    assert again.total_bars == 32


def test_segment_positions_snap_to_beats():
    s = Segment.from_dict(seg(start=17.1, length=7.9))
    assert (s.start_bar, s.length_bars, s.end_bar) == (17.0, 8.0, 25.0)


@pytest.mark.parametrize(
    "bad",
    [
        {"segments": [{"id": "x"}]},  # missing fields
        {"segments": [seg(length=0)]},  # zero length
        {"segments": [seg(), seg()]},  # duplicate ids
        {"settings": {"decks": 7}},  # more than 6 decks
        {"settings": {"decks": 0}},  # no decks
        {"settings": {"decks": 2, "jog_wheels": 7}},  # more than 6 jog wheels
        {"segments": [seg(deck=17)]},  # no such lane (MAX_LANES = 16)
        {"segments": [seg(at=0)]},  # before the start of the set
        [],  # not an object
    ],
)
def test_invalid_plans_rejected(bad):
    with pytest.raises(ValueError):
        Plan.from_dict(bad)


def test_blank_name_gets_default():
    assert Plan.from_dict({"name": "   "}).name == "Untitled set"


# ----- storage --------------------------------------------------------------------------


def test_save_list_load_delete(tmp_path):
    store = PlanStore(tmp_path)
    plan = store.save(Plan.from_dict({"name": "Mix 1", "segments": [seg()]}))
    assert plan.id and plan.created_at and plan.updated_at

    listed = store.list()
    assert [(p["id"], p["name"], p["segment_count"], p["total_bars"]) for p in listed] == [
        (plan.id, "Mix 1", 1, 16)
    ]
    assert store.load(plan.id).segments[0].label == "Bars 1-16"

    moved = store.delete(plan.id)
    assert moved.parent.name == ".trash" and moved.exists()
    assert store.list() == []
    with pytest.raises(FileNotFoundError):
        store.load(plan.id)


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", "", "x" * 65, "has.dot"])
def test_unsafe_plan_ids_rejected(tmp_path, bad_id):
    with pytest.raises(ValueError):
        PlanStore(tmp_path).load(bad_id)


def test_broken_file_listed_not_crashing(tmp_path):
    (tmp_path / "junk.json").write_text("{not json", encoding="utf-8")
    listed = PlanStore(tmp_path).list()
    assert listed[0]["id"] == "junk" and listed[0]["broken"]


# ----- version upgrade --------------------------------------------------------------------


def test_v1_plan_upgraded_to_deck_1_back_to_back():
    v1 = {"version": 1, "segments": [
        {"id": "a", "track_id": "1", "start_bar": 1, "length_bars": 8, "label": "Intro"},
        {"id": "b", "track_id": "1", "start_bar": 17, "length_bars": 16, "label": "Chorus"},
        {"id": "c", "track_id": "2", "start_bar": 1, "length_bars": 4, "label": "x"},
    ]}
    p = Plan.from_dict(v1)
    assert [(s.id, s.deck, s.set_bar) for s in p.segments] == [("a", 1, 1), ("b", 1, 9), ("c", 1, 25)]
    assert p.version == 2 and p.total_bars == 28


def test_segments_sorted_by_set_position():
    p = Plan.from_dict({"segments": [seg("late", at=33), seg("d2", deck=2, at=1), seg("d1", at=1)]})
    assert [s.id for s in p.segments] == ["d1", "d2", "late"]


def test_overlap_bars():
    a = Segment.from_dict(seg("a", at=1, length=16))
    b = Segment.from_dict(seg("b", deck=2, at=9, length=16))
    assert a.overlap_bars(b) == 8 and b.overlap_bars(a) == 8
    assert a.overlap_bars(Segment.from_dict(seg("c", at=17))) == 0


# ----- layout checks ----------------------------------------------------------------------


def test_clean_two_deck_blend_has_no_problems():
    m = messages({"segments": [
        seg("a", track_id="1", at=1, length=32),
        seg("b", track_id="2", deck=2, at=25, length=16),  # 8-bar blend
    ]})
    assert m == {}


def test_same_deck_overlap_is_error_on_both():
    m = messages({"segments": [seg("a", at=1, length=16), seg("b", track_id="2", at=9, length=16)]})
    assert m["a"][0][0] == "error" and "same lane" in m["a"][0][1]
    assert m["b"][0][0] == "error"


def at_once(m):
    """The blocks flagged for needing more decks at once than there are."""
    return {sid for sid, ws in m.items() for _, msg in ws if "need a deck at once" in msg}


def test_more_lanes_than_decks_is_fine_when_they_take_turns():
    # 2 decks, 3 lanes: lane 3 starts 4 bars after lane 1's track finishes, so it can go
    # on lane 1's deck (the solver picks which).
    segs = [seg("a", at=1, length=16), seg("b", track_id="2", deck=2, at=9, length=32),
            seg("c", track_id="3", deck=3, at=21, length=16)]
    assert at_once(messages({"settings": {"decks": 2}, "segments": segs})) == set()


def test_too_many_lanes_at_once():
    # 2 decks, but 3 lanes play together at bar 9
    segs = [seg("a", at=1, length=16), seg("b", track_id="2", deck=2, at=5, length=16),
            seg("c", track_id="3", deck=3, at=9, length=16)]
    m = messages({"settings": {"decks": 2}, "segments": segs})
    assert at_once(m) == {"a", "b", "c"}
    assert "At set bar 9, 3 lanes need a deck at once" in m["c"][0][1]


def test_no_time_to_load_counts_as_at_once():
    # lane 3 starts 1 bar after lane 1 finishes: too soon to load it on that deck
    segs = [seg("a", at=1, length=16), seg("b", track_id="2", deck=2, at=9, length=32),
            seg("c", track_id="3", deck=3, at=18, length=16)]
    assert at_once(messages({"settings": {"decks": 2}, "segments": segs})) == {"a", "b", "c"}


def test_same_track_two_decks_needs_instant_doubles():
    segs = [seg("a", at=1, length=16), seg("b", deck=2, at=9, length=16)]  # both track 1
    assert "Instant doubles" in messages({"segments": segs})["a"][0][1]
    assert messages({"segments": segs, "settings": {"instant_doubles": True}}) == {}


def test_same_track_continuing_on_same_deck_is_fine():
    # A jump within the same track on one deck (hot cue territory) is not a problem.
    assert messages({"segments": [seg("a", at=1, length=8), seg("b", start=33, at=9, length=8)]}) == {}


def test_deck_switching_track_instantly_is_error():
    m = messages({"segments": [seg("a", at=1, length=8), seg("b", track_id="2", at=9, length=8),
                               seg("c", track_id="3", deck=2, at=1, length=16)]})
    assert any("no time to load" in msg for _, msg in m["b"])


def test_hard_cut_between_decks_is_info():
    m = messages({"segments": [seg("a", at=1, length=16), seg("b", track_id="2", deck=2, at=17)]})
    assert m["b"] == [("info", m["b"][0][1])] and "Hard cut" in m["b"][0][1]


def test_silence_gap_is_plan_warning():
    m = messages({"segments": [seg("a", at=1, length=8), seg("b", track_id="2", deck=2, at=17)]})
    assert m[None] == [("warning", "Silence from set bar 9 to 17: nothing is playing.")]


# ----- track checks -----------------------------------------------------------------------


def test_track_checks_flag_missing_track_and_overrun():
    lib = FakeLibrary()  # track "1" has 32 bars, track "2" has no beatgrid
    m = messages({"settings": {"decks": 4}, "segments": [
        seg("ok", start=17, length=16, deck=1, at=1),  # bars 17-32: fine
        seg("over", start=25, length=16, deck=2, at=17, track_id="1"),  # runs to bar 41
        seg("gone", track_id="999", deck=3, at=33),
        seg("nogrid", track_id="2", deck=4, at=49),
    ]}, lib)
    text = {k: " ".join(msg for _, msg in v) for k, v in m.items()}
    assert "ok" not in text
    assert "past the end" in text["over"]
    assert "no longer in your Rekordbox library" in text["gone"]
    assert "no beatgrid" in text["nogrid"]


# ----- API ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    api.set_library(FakeLibrary())
    api.set_plan_store(PlanStore(tmp_path))
    with TestClient(api.app) as c:
        yield c


def test_plan_api_lifecycle(client):
    r = client.post("/api/plans", json={"name": "Set A", "segments": [seg()]})
    assert r.status_code == 200
    plan = r.json()["plan"]
    pid = plan["id"]
    assert r.json()["warnings"] == []

    plan["name"] = "Set A (edit)"
    plan["segments"].append(seg("s2", start=25, length=16, at=17))  # overruns the fake track
    r = client.put(f"/api/plans/{pid}", json=plan)
    assert r.status_code == 200
    assert r.json()["plan"]["created_at"] == plan["created_at"]
    assert [w["segment_id"] for w in r.json()["warnings"]] == ["s2"]

    assert [p["name"] for p in client.get("/api/plans").json()] == ["Set A (edit)"]
    assert client.get(f"/api/plans/{pid}").json()["plan"]["segments"][1]["id"] == "s2"

    assert client.delete(f"/api/plans/{pid}").status_code == 200
    assert client.get(f"/api/plans/{pid}").status_code == 404


def test_post_never_overwrites_existing(client):
    first = client.post("/api/plans", json={"name": "One"}).json()["plan"]
    second = client.post("/api/plans", json={**first, "name": "Two"}).json()["plan"]
    assert first["id"] != second["id"]
    assert len(client.get("/api/plans").json()) == 2


def test_check_endpoint_does_not_save(client):
    r = client.post("/api/plans/check", json={"segments": [seg("a"), seg("b", track_id="2", at=9)]})
    assert r.status_code == 200
    assert {w["segment_id"] for w in r.json()["warnings"]} >= {"a", "b"}
    assert client.get("/api/plans").json() == []


def test_plan_api_errors(client):
    assert client.post("/api/plans", json={"segments": [{"id": "x"}]}).status_code == 400
    assert client.put("/api/plans/nope", json={"name": "x"}).status_code == 404
    assert client.get("/api/plans/..%2Fevil").status_code in (400, 404)


# ----- solver + cheat sheet via the API (Phase 3) --------------------------------------


def test_responses_include_solution_and_saved_actions(client):
    # Fake track "1": 32 bars. Intro bars 1-16 then skip to bar 25 (+8 bars = beat jump)
    segs = [seg("a", start=1, length=16, at=1), seg("b", start=25, length=8, at=17)]
    r = client.post("/api/plans/check", json={"segments": segs}).json()
    kinds = [a["kind"] for a in r["solution"]["actions"]]
    # (load, go to the cue, PLAY, pads to BEAT JUMP mode, beat jump, stop)
    assert kinds == ["load", "cue", "start", "pad_mode", "beat_jump", "stop"]

    saved = client.post("/api/plans", json={"name": "Solve me", "segments": segs}).json()
    assert [a["kind"] for a in saved["plan"]["actions"]] == kinds  # stored with the plan


def test_cheatsheet_endpoints(client):
    segs = [seg("a", start=1, length=16, at=1), seg("b", start=25, length=8, at=17)]
    pid = client.post("/api/plans", json={"name": "Friday / Mix", "segments": segs}).json()["plan"]["id"]

    sheet = client.get(f"/api/plans/{pid}/cheatsheet").json()
    assert sheet["name"] == "Friday / Mix"
    # (loading is in the "Tracks on each deck" list at the top, not a step)
    assert [s["kind"] for s in sheet["steps"]] == ["cue", "start", "pad_mode", "beat_jump", "stop"]
    assert sheet["decks"][0]["tracks"][0]["title"] == "Roller"
    jump = next(s for s in sheet["steps"] if s["kind"] == "beat_jump")
    assert (jump["bar"], jump["beat"]) == (17, 1)
    assert "prep" not in sheet  # one transition at a time, on the fly: no prep list
    cue = next(s for s in sheet["steps"] if s["kind"] == "cue")
    assert cue["short"] == "Find track bar 1, press CUE"  # no memory cue there yet

    txt = client.get(f"/api/plans/{pid}/cheatsheet.txt")
    assert txt.status_code == 200
    assert "Friday  Mix - cheat sheet.txt" in txt.headers["content-disposition"]
    assert "STEPS" in txt.text and "beat jump forward 8 bars" in txt.text.lower()

    assert client.get("/api/plans/nope/cheatsheet").status_code == 404


def test_no_hard_cut_when_incoming_deck_was_already_playing():
    # Deck 2 blends in under deck 1's end (bars 9-16), then jumps to another part of its
    # own track exactly when deck 1 stops: deck 2 was already playing, so no hard cut.
    m = messages({"segments": [
        seg("a", track_id="1", at=1, length=16),
        seg("b1", track_id="2", deck=2, at=9, length=8),
        seg("b2", track_id="2", deck=2, start=17, at=17, length=8),
    ]})
    assert "b2" not in m


# ----- deck setup: decks and jog wheels ---------------------------------------------------


def test_old_plans_get_one_jog_wheel_per_deck():
    st = Plan.from_dict({"settings": {"decks": 4}}).settings
    assert (st.decks, st.jog_wheels, st.shared_jogs) == (4, 4, False)


@pytest.mark.parametrize("decks, jogs, expected", [
    (4, 2, [1, 2, 1, 2]),  # DDJ-FLX6-GT: 1/3 left, 2/4 right
    (6, 2, [1, 2, 1, 2, 1, 2]),
    (6, 3, [1, 2, 3, 1, 2, 3]),
    (3, 2, [1, 2, 1]),
    (3, 3, [1, 2, 3]),  # three CDJs
    (2, 4, [1, 2]),  # more jog wheels than tracks: still one each
])
def test_jog_wheel_sharing(decks, jogs, expected):
    st = Plan.from_dict({"settings": {"decks": decks, "jog_wheels": jogs}}).settings
    assert [st.jog_of(d) for d in range(1, decks + 1)] == expected


def test_settings_round_trip_with_jog_wheels():
    p = Plan.from_dict({"settings": {"decks": 6, "jog_wheels": 2}})
    again = Plan.from_dict(json.loads(json.dumps(p.to_dict())))
    assert (again.settings.decks, again.settings.jog_wheels) == (6, 2)


def test_a_lone_block_on_a_high_lane_is_fine():
    # lanes aren't decks: a block on lane 4 of a 3-deck setup just goes on some deck
    assert at_once(messages({"settings": {"decks": 3}, "segments": [seg("a", deck=4)]})) == set()



@pytest.mark.parametrize("settings, expected", [
    ({"decks": 2}, "2 tracks, one jog wheel each"),
    ({"decks": 2, "jog_wheels": 6}, "2 tracks, one jog wheel each"),
    ({"decks": 4, "jog_wheels": 2}, "4 tracks on 2 jog wheels: Left = Decks 1 & 3, Right = Decks 2 & 4"),
    ({"decks": 3, "jog_wheels": 2}, "3 tracks on 2 jog wheels: Left = Decks 1 & 3, Right = Deck 2"),
    ({"decks": 6, "jog_wheels": 3}, "6 tracks on 3 jog wheels: Jog 1 = Decks 1 & 4, Jog 2 = Decks 2 & 5, "
                                    "Jog 3 = Decks 3 & 6"),
])
def test_cheatsheet_setup_line(settings, expected):
    from djplanner.cheatsheet import setup_text
    assert setup_text(Plan.from_dict({"settings": settings})) == expected
