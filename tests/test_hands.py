"""Tests for the hands model (djplanner/hands.py) and controller layout (controllers.py)."""

from djplanner import hands
from djplanner.controllers import Layout, distance
from djplanner.models import Action

L = Layout(4, 2)


def act(kind, deck, bar, **kw):
    return Action(kind=kind, deck=deck, track_id="t", set_bar=bar, text="Do it", **kw)


def test_flx6_layout_distances():
    assert round(distance(L.fader(1), L.fader(2)), 1) == 3.8  # neighbouring channels
    assert L.fader(3)[0] < L.fader(1)[0] < L.fader(2)[0] < L.fader(4)[0]  # order 3 1 2 4
    assert L.side_of(1) == L.side_of(3) == 1 and L.side_of(2) == L.side_of(4) == 2
    assert distance(L.side_control(1, "play"), L.pad(2)) > 50  # across the controller


def test_travel_is_free_nearby_and_grows_with_distance():
    assert hands.travel(L.fader(1), L.fader(4)) == 0  # sliding along the fader row
    far = hands.travel(L.side_control(1, "loop_in"), L.side_control(2, "play"))
    assert 1.0 < far < 1.4


def test_one_hand_cant_cross_the_controller_in_half_a_beat():
    # left hand presses left PLAY at bar 9, right hand right PLAY at bar 9; then the
    # far-right pads at bar 9.25 (0.34 s later at 174 BPM): whichever hand goes, too tight
    acts = [act("start", 1, 9), act("start", 2, 9), act("hot_cue", 3, 9.25, pad="H")]
    _, issues = hands.simulate(acts, L, 174)
    assert any(i["message"].startswith("Too tight") for i in issues)


def test_same_side_work_is_fine():
    acts = [act("start", 1, 9), act("hot_cue", 1, 11, pad="A"), act("fader_up", 1, 13)]
    assert not hands.simulate(acts, L, 174)[1]


def test_pad_mode_changes_are_tracked_per_deck():
    acts = [act("hot_cue", 1, 5, pad="A"), act("beat_jump", 1, 9, params={"presses": 1}),
            act("beat_jump", 1, 13, params={"presses": 1}), act("hot_cue", 1, 17, pad="B")]
    out, _ = hands.simulate(acts, L, 174)
    assert [a.params.get("pad_mode") for a in acts] == [None, "beat_jump", None, "hot_cue"]
    # each mode change is its own step, just before the action that needs it
    assert [a.kind for a in out] == ["hot_cue", "pad_mode", "beat_jump", "beat_jump", "pad_mode", "hot_cue"]
    assert out[1].text == "Deck 1: pads → BEAT JUMP mode" and out[4].params["mode"] == "hot_cue"


def test_relaxed_action_goes_where_a_hand_is_free_and_close():
    # right hand busy on Deck 2 around bar 20; a relaxed Deck 1 beat jump with window 10-30
    fixed = [act("start", 2, 19), act("hot_cue", 2, 20, pad="A"), act("hot_cue", 2, 21, pad="B")]
    jump = act("beat_jump", 1, 10, params={"presses": 1, "window": [10, 30]})
    acts = fixed + [jump]
    hands.place_relaxed(acts, L, 174)
    assert 10 <= jump.set_bar <= 30
    assert not (18.5 <= jump.set_bar <= 21.5)  # not in the middle of the busy bit
