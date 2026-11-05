"""Tests for the cheat sheet's get-ready groups (djplanner/cheatsheet.py)."""

from djplanner.cheatsheet import build_cheatsheet, cheatsheet_text
from djplanner.models import Plan
from djplanner.solver import solve
from test_solver import block, chain, fake_track, lib


def og_mix():
    # The user's "og mix" shape: T12 (1) to its pre-drop, T17 (2) on deck 2, T12
    # back at 52.5, T13 (4) on deck 4 at 53 (4 decks on 2 jog wheels)
    segs = (chain((33, 36), track="1") + [block(17, 16, at=37, deck=2, track="2")]
            + chain((68.5, 0.5), (69, 8), at=52.5, track="1")
            + [block(17, 16, at=53, deck=4, track="4")])
    plan = Plan.from_dict({"name": "og", "segments": segs, "settings": {"decks": 4, "jog_wheels": 2}})
    get = lib(fake_track("1", bars=128), fake_track("2"), fake_track("4"))
    return plan, solve(plan, get), get


def test_each_faded_in_track_gets_a_get_ready_group():
    plan, sol, get = og_mix()
    sheet = build_cheatsheet(plan, sol, get)
    harb = next(g for g in sheet["groups"] if g["deck"] == 4)
    kinds = [sheet["steps"][i]["kind"] for i in harb["steps"]]
    assert kinds[-1] == "fader_up" and {"cue", "start", "deck_switch"} <= set(kinds)
    assert harb["deadline"] == 53 and "Track 2" in harb["while"]
    assert all(sheet["steps"][i]["group"] is not None for i in harb["steps"])


def test_steps_are_short_with_the_details_separate():
    plan, sol, get = og_mix()
    sheet = build_cheatsheet(plan, sol, get)
    play = next(s for s in sheet["steps"] if s["kind"] == "start" and s["deck"] == 4)
    assert play["short"] == "PLAY (fader DOWN)"
    assert play["detail"].startswith("4 bars early") and "53.1" in play["detail"]
    cue = next(s for s in sheet["steps"] if s["kind"] == "cue" and s["deck"] == 4)
    assert cue["short"] == "Find track bar 13, press CUE" and cue["when"] == "by"  # no cue there yet
    up = next(s for s in sheet["steps"] if s["kind"] == "fader_up" and s["deck"] == 1)
    assert (up["short"], up["bar"], up["beat"]) == ("Fader UP", 52, 3)


def test_tracks_on_each_deck_at_the_top_and_no_load_steps():
    plan, sol, get = og_mix()
    sheet = build_cheatsheet(plan, sol, get)
    assert not any(s["kind"] == "load" for s in sheet["steps"])
    decks = {d["deck"]: [t["title"] for t in d["tracks"]] for d in sheet["decks"]}
    assert decks == {1: ["Track 1"], 2: ["Track 2"], 4: ["Track 4"]}
    assert sheet["decks"][0]["side"] == "Left"
    assert "TRACKS ON EACH DECK" in cheatsheet_text(sheet)


def test_relaxed_steps_show_their_window():
    plan, sol, get = og_mix()
    sheet = build_cheatsheet(plan, sol, get)
    jump = next(s for s in sheet["steps"] if s["kind"] == "beat_jump")
    assert jump["when"] == "between" and jump["window"] == [37, 51]
    assert jump["short"] == "Beat jump back 16 bars"
    text = cheatsheet_text(sheet)
    assert jump["before"] == "51.3"  # shown as "51.3 before"
    assert "by 51.3" in text and "Beat jump back 16 bars" in text
