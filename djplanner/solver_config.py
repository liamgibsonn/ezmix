"""
Solver settings: how the solver scores different ways of performing your plan.

The solver tries every sensible way of playing each part of the plan (hot cue jump,
beat jump, loop, ...) and picks the route with the LOWEST total cost. Lower cost =
easier to perform. Change the numbers below to change its preferences, e.g. raise
`beat_jump_timing` if you'd rather use pads than beat jumps.

Costs are in rough "button presses": 1.0 = one timed press during the set.
"""

COSTS = {
    # Every button press you have to make at a precise moment during the set.
    "action": 1.0,
    # Setting up a NEW hot cue before the set (prep work, and it uses up a pad).
    "new_hot_cue": 0.6,
    # Setting up a NEW memory cue before the set (prep work, no pad needed).
    "new_memory_cue": 0.3,
    # Beat jumps have to be pressed right on the beat (hot cues are snapped to the beat
    # by quantize, so they're more forgiving). Extra cost per beat-jump press.
    "beat_jump_timing": 0.2,
    # A second beat-jump press straight after the first (e.g. +32 then +16 beats).
    "extra_beat_jump_press": 0.5,
    # Setting a loop by hand (LOOP IN + LOOP OUT) instead of one auto beat-loop button.
    "manual_loop": 1.0,
    # Two actions on the same deck less than AWKWARD_WINDOW_BARS apart (hard to do).
    "tight_timing": 3.0,
    # Starting a track by pressing PLAY / a pad right on the bar it has to be heard
    # (instead of a pre-roll: started early with the fader down, then just fader up).
    # The very first track of the set doesn't pay this.
    "exact_start": 1.5,
    # Moving a channel fader on a bar (easier than hitting a button on the beat).
    "fader": 0.5,
    # Something you can do any time in a window of a few bars (e.g. a beat jump while
    # the deck is muted): no timing pressure.
    "relaxed_action": 0.3,
    # Jumping/starting somewhere that isn't the start of a Rekordbox phrase.
    "not_phrase_aligned": 0.5,
    # No way to play it live: has to be rendered as an audio edit instead.
    "render_fallback": 100.0,
}

# Actions on ONE deck closer together than this (in bars) cost extra while solving
# (the cross-deck "two hands" check uses the plan's reaction time setting instead).
AWKWARD_WINDOW_BARS = 1.0

# How hard a moment is for your HANDS is worked out from where the controls are on the
# controller and how far your hands move (hands.py, controllers.py), set up for
# BEGINNERS (the user: beginners press the wrong buttons and panic; aim at them).

# Pre-roll lengths to try (bars), longest first, capped at the plan's preroll_bars:
# if there isn't room for the full pre-roll, a shorter one beats none.
PREROLL_FALLBACK_BARS = [8, 4, 2, 1]

# Shared jog wheels (e.g. 4 decks on 2 jog wheels): if one side has to do something on
# one deck, switch to the other deck and do something there within less than this many
# bars, it's flagged as too tight.
SWITCH_WINDOW_BARS = 2.0

# Lanes aren't decks (lanes.py): two lanes can share a deck if one's track has finished
# at least this many bars before the other's starts (time to load and cue the next one).
LOAD_GAP_BARS = 2.0

# Beat jump sizes available (in beats). Rekordbox lets you choose the size; these are
# its usual options. Positive and negative (backwards) jumps both work.
BEAT_JUMP_SIZES_BEATS = [1, 2, 4, 8, 16, 32, 64]

# Most beat-jump presses to chain for one jump (e.g. 2 = "+32 then +16").
MAX_BEAT_JUMP_PRESSES = 2

# Auto beat-loop lengths available (in beats). 4 beats = 1 bar.
AUTO_LOOP_SIZES_BEATS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]

# Hot cue pads per track (A-H).
HOT_CUE_PADS = ["A", "B", "C", "D", "E", "F", "G", "H"]

# How close (in bars) an existing cue must be to a target to count as "on" it.
CUE_MATCH_TOLERANCE_BARS = 1 / 16
