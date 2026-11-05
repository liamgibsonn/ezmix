"""
Solver tests with small fake tracks.

Fake track layout (64 bars, phrases every 16 bars):
    Intro 1-16 | Chorus 1 17-32 | Down 33-48 | Chorus 2 49-64
"""

import pytest

from djplanner import solver_config as cfg
from djplanner.beatgrid import Beatgrid
from djplanner.models import Cue, Phrase, Plan, Track, TrackSummary
from djplanner.solver import beat_jump_presses, solve, solve_best_decks, split_into_runs

PHRASES = [("intro", "Intro", 1), ("chorus", "Chorus 1", 17), ("down", "Down", 33), ("chorus", "Chorus 2", 49)]


def fake_track(tid="1", cues=(), bars=64):
    grid = Beatgrid.constant(174, bars=bars)
    summary = TrackSummary(
        id=tid, title=f"Track {tid}", artist="", bpm=174, key="Fm", camelot="4A",
        duration_s=bars * 4 * 60 / 174, file_path="x", original_path="x", file_exists=True,
        has_beatgrid=True, has_phrases=True,
    )
    phrases = [Phrase(k, label, float(start), 16.0) for k, label, start in PHRASES]
    cue_objs = [Cue(type=t, pad=pad, raw_kind=1, time_s=0, bar=float(bar)) for t, pad, bar in cues]
    return Track(summary=summary, beatgrid=grid, phrases=phrases, cues=cue_objs)


def lib(*tracks):
    by_id = {t.id: t for t in tracks}

    def get(tid):
        return by_id[tid]  # KeyError if missing, like the real library

    return get


_ids = iter(range(10_000))


def block(start, length, at, deck=1, track="1", label=None):
    """A block playing TRACK bars start..start+length-1 at SET bar `at`."""
    return {"id": f"b{next(_ids)}", "track_id": track, "start_bar": start, "length_bars": length,
            "label": label or f"bars {start}+{length}", "deck": deck, "set_bar": at,
            "track_title": f"Track {track}"}


def chain(*ranges, deck=1, track="1", at=1):
    """Blocks played back to back on one deck: chain((1,16), (49,16)) etc."""
    out = []
    for start, length in ranges:
        out.append(block(start, length, at, deck, track))
        at += length
    return out


def run(segments, *tracks, **settings):
    plan = Plan.from_dict({"segments": segments, "settings": settings or None})
    return solve(plan, lib(*(tracks or (fake_track(),))))


def kinds(sol, include=("start", "hot_cue", "beat_jump", "loop_in", "loop_out", "loop_exit", "render")):
    return [a.kind for a in sol.actions if a.kind in include]


# ----- helpers --------------------------------------------------------------------------


@pytest.mark.parametrize("bars, expected", [
    (16, [64]), (-8, [-32]), (12, [32, 16]), (3, [8, 4]), (0.75, [2, 1]), (0.5, [2]),
    (100, None),  # 400 beats: needs more than 2 presses
    (0, None), (0.1, None),
])
def test_beat_jump_presses(bars, expected):
    assert beat_jump_presses(bars) == expected


def test_split_into_runs():
    segs = chain((1, 16), (17, 16)) + [block(1, 16, at=49)] + [block(1, 16, 1, deck=2, track="2")]
    plan = Plan.from_dict({"segments": segs})
    runs = split_into_runs(plan)
    assert [(r.deck, r.track_id, len(r.blocks)) for r in runs] == [(1, "1", 2), (2, "2", 1), (1, "1", 1)]


# ----- single-track moves -------------------------------------------------------------


def test_natural_continuation_needs_nothing():
    sol = run(chain((1, 16), (17, 16), (33, 32)))
    assert kinds(sol) == ["start"]  # just press play


def test_start_uses_memory_cue_not_a_pad():
    sol = run(chain((17, 16)))
    start = next(a for a in sol.actions if a.kind == "start")
    assert "PLAY" in start.text
    assert [(c.type, c.track_bar) for c in sol.cues] == [("memory", 17.0)]


def test_forward_power_of_two_skip_is_beat_jump():
    sol = run(chain((1, 16), (33, 16)))  # skip 16 bars = 64 beats
    jump = next(a for a in sol.actions if a.kind == "beat_jump")
    assert jump.params["beats"] == [64]
    assert jump.set_bar == 17  # right on the 1 where the skip happens


def test_awkward_skip_uses_new_hot_cue_on_free_pad():
    # skip 13 bars (52 beats): no 1-2 press beat jump -> hot cue
    sol = run(chain((1, 16), (30, 8)))
    hc = next(a for a in sol.actions if a.kind == "hot_cue")
    assert hc.pad == "A" and hc.set_bar == 16.75  # beat 4 of bar 16
    assert "pad A" in hc.text
    assert any(c.type == "hot" and c.pad == "A" and c.track_bar == 30 and not c.existing for c in sol.cues)


def test_existing_cue_is_reused_and_never_overwritten():
    # Your pads A and B are taken; A is on bar 30.
    t = fake_track(cues=[("hot", "A", 30), ("hot", "B", 5)])
    sol = run(chain((1, 16), (30, 8), (3, 4)), t)
    hot = [a for a in sol.actions if a.kind == "hot_cue"]
    assert hot[0].pad == "A"  # your cue, reused
    assert hot[1].pad == "C"  # new cue goes on the first FREE pad
    assert all(c.pad not in ("A", "B") for c in sol.cues if not c.existing)
    assert any(c.existing and c.pad == "A" for c in sol.cues)


def test_repeated_jump_target_reuses_the_same_new_pad():
    # Jump to bar 30 twice: one new cue, used twice
    sol = run(chain((1, 16), (30, 4), (1, 16), (30, 4)))
    hot = [a for a in sol.actions if a.kind == "hot_cue" and a.track_bar == 30]
    assert len(hot) == 2 and hot[0].pad == hot[1].pad
    # Bar 1 is needed twice too (start + jump back), so the cheapest route puts a pad
    # there and starts from it, instead of a memory cue PLUS a pad: 2 new pads total.
    start = next(a for a in sol.actions if a.kind == "start")
    back = next(a for a in sol.actions if a.kind == "hot_cue" and a.track_bar == 1)
    assert start.pad == back.pad
    assert sorted(c.track_bar for c in sol.cues if c.type == "hot") == [1.0, 30.0]


def test_repeat_twice_uses_a_loop():
    # Chorus 1 then two more passes of its last 8 bars -> auto loop (8 bars = 32 beats)
    sol = run(chain((17, 16), (25, 8), (25, 8)))
    assert kinds(sol) == ["start", "loop_in", "loop_exit"]
    loop_in = next(a for a in sol.actions if a.kind == "loop_in")
    exit_ = next(a for a in sol.actions if a.kind == "loop_exit")
    assert {k: loop_in.params[k] for k in ("loop_bars", "passes", "method", "beats")} == \
        {"loop_bars": 8, "passes": 3, "method": "auto", "beats": 32}
    assert loop_in.set_bar == 9  # deck reaches track bar 25 at set bar 9
    assert exit_.set_bar == 25  # start of the last pass (set bars 25-32)


def test_single_repeat_prefers_one_beat_jump_back():
    sol = run(chain((17, 16), (25, 8)))
    assert kinds(sol) == ["start", "beat_jump"]
    assert next(a for a in sol.actions if a.kind == "beat_jump").params["beats"] == [-32]


def test_non_power_of_two_loop_is_manual():
    # repeat the last 3 bars three times: 12 beats isn't an auto-loop size
    sol = run(chain((1, 16), (14, 3), (14, 3), (14, 3)))
    assert kinds(sol)[:3] == ["start", "loop_in", "loop_out"]
    assert next(a for a in sol.actions if a.kind == "loop_in").params["method"] == "manual"


def test_out_of_pads_falls_back_to_render():
    t = fake_track(cues=[("hot", p, 60 + i * 0.25) for i, p in enumerate(cfg.HOT_CUE_PADS)])
    sol = run(chain((1, 16), (30, 4)), t)  # needs a hot cue; all 8 pads are yours
    assert "render" in kinds(sol)
    assert any(i["level"] == "error" for i in sol.issues)


def test_unknown_pad_cues_reduce_free_pads():
    t = fake_track(cues=[("hot", None, 60)] + [("hot", p, 61) for p in "ABCDEFG"])
    sol = run(chain((1, 16), (30, 4)), t)  # 7 known + 1 unknown = all pads taken
    assert "render" in kinds(sol)


# ----- runs, decks, timing -----------------------------------------------------------------


def test_load_start_stop_for_each_run():
    # Deck 2's block starts at track bar 1: no room for a pre-roll, so a direct start
    segs = chain((1, 32)) + [block(1, 32, at=25, deck=2, track="2")]
    sol = run(segs, fake_track("1"), fake_track("2"))
    summary = [(a.kind, a.deck, a.set_bar) for a in sol.actions]
    assert summary == [
        ("load", 1, 1), ("cue", 1, 1), ("start", 1, 1),
        ("load", 2, 25), ("cue", 2, 25), ("start", 2, 25),
        ("stop", 1, 33),
        ("stop", 2, 57),
    ]


def test_preroll_start_with_fader_down_then_fader_up():
    # Deck 2 comes in at set bar 25 with track bar 17: start it 4 bars early from a cue
    # at track bar 13 (fader down), then just fader up at 25
    segs = chain((1, 32)) + [block(17, 16, at=25, deck=2, track="2")]
    sol = run(segs, fake_track("1"), fake_track("2"))
    d2 = [(a.kind, a.set_bar, a.track_bar) for a in sol.actions if a.deck == 2 and a.kind != "load"]
    # go to the cue (its own step), PLAY with the fader down, then fader up
    assert d2[:3] == [("cue", 21, 13), ("start", 21, 13), ("fader_up", 25, 17)]
    assert any(c.type == "memory" and c.track_bar == 13 for c in sol.cues)


def test_preroll_shortens_when_the_deck_is_busy():
    # Deck 2 plays track 3 until set bar 23, then track 2 must come in at 25: only 2 bars free
    segs = (chain((1, 32)) + [block(1, 22, at=1, deck=2, track="3")]
            + [block(17, 16, at=25, deck=2, track="2")])
    sol = run(segs, fake_track("1"), fake_track("2"), fake_track("3"))
    start = next(a for a in sol.actions if a.kind == "start" and a.track_id == "2")
    assert (start.set_bar, start.track_bar, start.params.get("preroll")) == (23, 15, 2)


def test_preroll_off_means_a_direct_start():
    segs = chain((1, 32)) + [block(17, 16, at=25, deck=2, track="2")]
    plan = Plan.from_dict({"segments": segs, "settings": {"preroll_bars": 0}})
    sol = solve(plan, lib(fake_track("1"), fake_track("2")))
    start = next(a for a in sol.actions if a.kind == "start" and a.deck == 2)
    assert (start.set_bar, start.track_bar) == (25, 17)
    assert not any(a.kind == "fader_up" for a in sol.actions)


def test_same_track_comes_back_by_running_muted_and_a_relaxed_beat_jump():
    # The user's "og mix": T12 plays to the end of its pre-drop (track 65-68 at set
    # 33-36), something else plays, then T12 comes back at set 52.5 with track 68.5.
    # Left running muted from set 37, it would be at 84.5: a beat jump of -16 bars, any
    # time in the gap, then fader up. (One button, no timing pressure.)
    segs = (chain((33, 36)) + [block(1, 16, at=37, deck=2, track="2")]
            + chain((68.5, 0.5), (69, 8), at=52.5))
    sol = run(segs, fake_track("1", bars=128), fake_track("2"))
    d1 = [a for a in sol.actions if a.deck == 1]
    kinds_d1 = [a.kind for a in d1]
    assert "fader_down" in kinds_d1 and "stop" in kinds_d1  # stop only at the very end
    jump = next(a for a in d1 if a.kind == "beat_jump")
    assert jump.params["beats"] == [-64] and jump.params["window"] == [37, 51.5]
    assert 37 <= jump.set_bar <= 51.5 and "any time between" in jump.text
    up = next(a for a in d1 if a.kind == "fader_up")
    assert up.set_bar == 52.5 and "on beat 3" in up.text
    assert kinds_d1.count("load") == 1  # it's still loaded: no second load


def test_pads_shared_across_runs_of_same_track():
    # Track 1 played twice (two runs), both needing a jump to bar 30: one cue
    segs = chain((1, 16), (30, 4)) + chain((1, 16), (30, 4), at=41)
    sol = run(segs)
    pads = {a.pad for a in sol.actions if a.kind == "hot_cue"}
    assert pads == {"A"}


def test_three_presses_at_once_is_too_tight_for_two_hands():
    # three PLAY presses at the same moment (pre-roll off): decks 1, 2 and 3 at bar 9
    segs = [block(1, 16, at=9, deck=1, track="1"), block(1, 16, at=9, deck=2, track="2"),
            block(1, 16, at=9, deck=3, track="3")]
    plan = Plan.from_dict({"segments": segs, "settings": {"decks": 3, "jog_wheels": 3, "preroll_bars": 0}})
    sol = solve(plan, lib(fake_track("1"), fake_track("2"), fake_track("3")))
    assert any(i["level"] == "warning" and i["message"].startswith("Too tight") for i in sol.issues)
    assert any("awkward" in a.flags for a in sol.actions)


def test_fader_after_fader_costs_nothing():
    # The user's og-mix moment: Deck 2 out + Deck 1 in at 52.5, Deck 4 in at 53. Faders
    # right next to each other: no warnings, no notes.
    segs = (chain((33, 36), track="1") + [block(1, 15.5, at=37, deck=2, track="2")]
            + chain((68.5, 0.5), (69, 8), at=52.5, track="1")
            + [block(17, 16, at=53, deck=4, track="4")])
    plan = Plan.from_dict({"segments": segs, "settings": {"decks": 4, "jog_wheels": 2}})
    sol = solve(plan, lib(fake_track("1", bars=128), fake_track("2"), fake_track("4")))
    around = [a for a in sol.actions if 52 <= a.set_bar <= 53.5]
    assert {a.kind for a in around} <= {"stop", "fader_up", "fader_down"}
    assert not [i for i in sol.issues if "52.5" in i["message"] or "53" in i["message"]]


def test_pad_mode_change_is_its_own_step():
    # a muted beat jump needs BEAT JUMP pad mode (pads start in HOT CUE mode)
    segs = (chain((33, 36), track="1") + [block(1, 16, at=37, deck=2, track="2")]
            + chain((68.5, 0.5), (69, 8), at=52.5, track="1"))
    sol = run(segs, fake_track("1", bars=128), fake_track("2"))
    jump = next(a for a in sol.actions if a.kind == "beat_jump")
    mode = sol.actions[sol.actions.index(jump) - 1]
    assert mode.kind == "pad_mode" and mode.params["mode"] == "beat_jump" and mode.deck == 1
    assert jump.params["pad_mode"] == "beat_jump" and jump.params["hand"] == "left"


def test_one_thing_at_a_time_is_fine():
    sol = run(chain((1, 16), (17, 16)))
    assert not sol.issues


def test_missing_track_still_solves():
    plan = Plan.from_dict({"segments": chain((1, 16), (30, 4), track="999")})
    sol = solve(plan, lib())
    assert "hot_cue" in kinds(sol)


def test_costs_follow_config(monkeypatch):
    # Make beat jumps very expensive: the solver should switch to a pad instead.
    monkeypatch.setitem(cfg.COSTS, "beat_jump_timing", 10.0)
    sol = run(chain((1, 16), (33, 16)))
    assert "hot_cue" in kinds(sol) and "beat_jump" not in kinds(sol)


def test_solution_serialises():
    d = run(chain((1, 16), (30, 4))).to_dict()
    assert {"actions", "cues", "cost", "issues"} <= d.keys()
    assert d["actions"][0]["kind"] == "load"


# ----- shared jog wheels (e.g. 4 decks on a 2-jog controller) ----------------------------


def switches(sol):
    return [a for a in sol.actions if a.kind == "deck_switch"]


def test_no_switches_with_one_jog_wheel_per_deck():
    segs = [block(1, 16, 1, deck=1)] + [block(1, 16, 9, deck=3, track="2")]
    sol = run(segs, fake_track("1"), fake_track("2"), decks=4)
    assert switches(sol) == []


def test_switch_added_before_deck_three():
    # Deck 1 starts at bar 1; Deck 3 (same left jog wheel) starts at bar 9.
    segs = [block(1, 16, 1, deck=1)] + [block(1, 16, 9, deck=3, track="2")]
    sol = run(segs, fake_track("1"), fake_track("2"), decks=4, jog_wheels=2)
    sw = switches(sol)
    assert len(sw) == 1
    assert sw[0].deck == 3 and sw[0].params["from_deck"] == 1
    assert sw[0].text.startswith("Left side → Deck 3")
    # placed in its window: after Deck 1's start (bar 1), before Deck 3's start (bar 9)
    w0, w1 = sw[0].params["window"]
    assert (w0, w1) == (1.25, 8.75) and w0 <= sw[0].set_bar <= w1
    start3 = next(a for a in sol.actions if a.kind == "start" and a.deck == 3)
    assert sol.actions.index(sw[0]) < sol.actions.index(start3)
    assert not any("deck switch" in x["message"] for x in sol.issues)  # 8 bars is plenty


def test_no_switch_for_the_other_side():
    # Decks 1 and 2 are on different jog wheels: nothing to switch.
    segs = [block(1, 16, 1, deck=1)] + [block(1, 16, 9, deck=2, track="2")]
    sol = run(segs, fake_track("1"), fake_track("2"), decks=4, jog_wheels=2)
    assert switches(sol) == []


def test_switch_back_and_forth():
    # Deck 3 plays alone first, then Deck 1, then a jump back on Deck 3.
    segs = (chain((1, 16), (49, 16), deck=3, track="2", at=1)
            + [block(1, 16, 9, deck=1)])
    sol = run(segs, fake_track("1"), fake_track("2"), decks=4, jog_wheels=2)
    sw = switches(sol)
    assert [a.deck for a in sw] == [3, 1, 3]
    # each one in its window (after the side's last use on the other deck, before it's needed)
    for a in sw:
        w0, w1 = a.params["window"]
        assert w0 <= a.set_bar <= w1
    assert sw[1].set_bar < 9  # left side to Deck 1 before Deck 1 starts at 9
    assert 9 < sw[2].set_bar < 16.75  # back to Deck 3 after that, before its jump at 16.75


def test_tight_switch_is_flagged():
    # Deck 1 jumps at bar 17 (pad on beat 4 of bar 16), Deck 3 starts at bar 17:
    # one beat to switch sides.
    segs = chain((1, 16), (49, 16), deck=1) + [block(1, 16, 17, deck=3, track="2")]
    sol = run(segs, fake_track("1"), fake_track("2"), decks=4, jog_wheels=2)
    sw = switches(sol)
    assert len(sw) == 1 and "awkward" in sw[0].flags
    msg = next(x["message"] for x in sol.issues if "deck switch" in x["message"])
    assert "Left side" in msg and "Deck 2, Deck 4" in msg



def assert_each_side_on_the_right_deck(sol, decks=4, jog_wheels=2):
    """Walk through the steps like the DJ would: every deck-control step must happen
    while its side is switched to that deck (sides start on Decks 1 and 2)."""
    jog_of = lambda d: (d - 1) % jog_wheels + 1
    selected = {j: j for j in range(1, jog_wheels + 1)}
    for a in sol.actions:
        if a.kind == "deck_switch":
            selected[jog_of(a.deck)] = a.deck
        elif a.kind in ("start", "hot_cue", "beat_jump", "loop_in", "loop_out", "loop_exit"):
            assert selected[jog_of(a.deck)] == a.deck, f"{a.kind} on Deck {a.deck} at {a.set_bar}"


def test_relaxed_beat_jump_stays_before_the_switch_away():
    # Deck 2 plays bars 1-16, goes muted, comes back to bar 1 at set bar 33 (a relaxed
    # beat jump any time in 17-32). Deck 4 (same right side) starts at 25: the jump must
    # be done before the right side switches to Deck 4 (it used to drift after it).
    segs = [block(1, 16, 1, deck=2), block(1, 16, 33, deck=2), block(1, 16, 25, deck=4, track="2")]
    sol = run(segs, fake_track("1"), fake_track("2"), decks=4, jog_wheels=2)
    assert "beat_jump" in kinds(sol)
    assert_each_side_on_the_right_deck(sol)
    jump = next(a for a in sol.actions if a.kind == "beat_jump")
    assert jump.params["window"][1] <= 25 - cfg.SWITCH_WINDOW_BARS


def test_best_decks_moves_a_lane_to_save_a_switch():
    # Lanes 1 and 3 are both on the left jog wheel: playing them as Decks 1 and 3 needs
    # a deck switch. Moving one of them to the right side saves it.
    segs = [block(1, 16, 1, deck=1), block(1, 16, 9, deck=3, track="2")]
    plan = Plan.from_dict({"segments": segs, "settings": {"decks": 4, "jog_wheels": 2}})
    played, sol = solve_best_decks(plan, lib(fake_track("1"), fake_track("2")))
    assert switches(sol) == []
    assert sol.deck_map[1] % 2 != sol.deck_map[3] % 2  # different sides
    # the returned plan has the same blocks, on their physical decks
    assert {s.id: s.deck for s in played.segments} == {s.id: sol.deck_map[s.deck] for s in plan.segments}
    assert sol.to_dict()["deck_map"] == {str(k): v for k, v in sol.deck_map.items()}


def test_best_decks_lets_lanes_take_turns_on_a_deck():
    # 2 decks, 3 lanes: lane 3 comes in after lane 1 has finished, so it reuses its deck.
    segs = [block(1, 16, 1, deck=1), block(1, 32, 9, deck=2, track="2"),
            block(1, 16, 21, deck=3, track="3")]
    plan = Plan.from_dict({"segments": segs, "settings": {"decks": 2}})
    played, sol = solve_best_decks(plan, lib(fake_track("1"), fake_track("2"), fake_track("3")))
    assert sol.deck_map[1] == sol.deck_map[3] != sol.deck_map[2]
    assert {s.deck for s in played.segments} <= {1, 2}
    loads = [a for a in sol.actions if a.kind == "load"]
    assert [a.deck for a in loads].count(sol.deck_map[1]) == 2  # loaded twice: track 1, then 3


def test_best_decks_falls_back_when_too_many_play_at_once():
    # 3 lanes at once on 2 decks: can't be placed (the plan checks say so), but it still solves
    segs = [block(1, 16, 1, deck=1), block(1, 16, 5, deck=2, track="2"),
            block(1, 16, 9, deck=3, track="3")]
    plan = Plan.from_dict({"segments": segs, "settings": {"decks": 2}})
    _, sol = solve_best_decks(plan, lib(fake_track("1"), fake_track("2"), fake_track("3")))
    assert sol.deck_map == {1: 1, 2: 2, 3: 3}


def test_best_decks_keeps_your_lanes_when_nothing_is_gained():
    # Decks 1 and 2 are on different sides already: nothing to gain by moving them.
    segs = [block(1, 16, 1, deck=1), block(1, 16, 9, deck=2, track="2")]
    plan = Plan.from_dict({"segments": segs, "settings": {"decks": 4, "jog_wheels": 2}})
    _, sol = solve_best_decks(plan, lib(fake_track("1"), fake_track("2")))
    assert sol.deck_map == {1: 1, 2: 2}


# ----- doubles: two tracks playing together never share a jog wheel (user) -------------


def doubles(sol):
    return [i for i in sol.issues if i["message"].startswith("Double on one jog wheel")]


def test_double_on_one_jog_wheel_is_flagged():
    # Decks 1 and 3 (both on the left jog wheel) play together for bars 9-16.
    segs = [block(1, 16, 1, deck=1), block(1, 16, 9, deck=3, track="2")]
    sol = run(segs, fake_track("1"), fake_track("2"), decks=4, jog_wheels=2)
    [w] = doubles(sol)
    assert "Deck 1 and Deck 3" in w["message"] and "set bar 9 until 17" in w["message"]
    assert sol.actions[w["action_index"]].deck == 3


def test_no_double_warning_on_different_sides_or_without_overlap():
    side_by_side = [block(1, 16, 1, deck=1), block(1, 16, 9, deck=2, track="2")]
    one_after_other = [block(1, 16, 1, deck=1), block(1, 16, 17, deck=3, track="2")]
    for segs in (side_by_side, one_after_other):
        sol = run(segs, fake_track("1"), fake_track("2"), decks=4, jog_wheels=2)
        assert doubles(sol) == []


def test_best_decks_never_puts_a_double_on_one_jog_wheel():
    # Lane 1 plays all through; lane 2 doubles with it for bars 1-16, then lane 3 for
    # bars 17-32. Putting lane 3 on Deck 3 would save a deck switch, but Deck 3 shares
    # the left jog wheel with Deck 1: a double on one jog. The planner takes the switch.
    segs = [block(1, 32, 1, deck=1), block(1, 16, 1, deck=2, track="2"),
            block(1, 16, 17, deck=3, track="3")]
    plan = Plan.from_dict({"segments": segs, "settings": {"decks": 4, "jog_wheels": 2}})
    assert doubles(solve(plan, lib(fake_track("1"), fake_track("2"), fake_track("3"))))  # as drawn
    _, sol = solve_best_decks(plan, lib(fake_track("1"), fake_track("2"), fake_track("3")))
    assert doubles(sol) == []
    assert sol.deck_map[1] % 2 != sol.deck_map[2] % 2 and sol.deck_map[1] % 2 != sol.deck_map[3] % 2
    assert len(switches(sol)) == 1
