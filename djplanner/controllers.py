"""
Where the controls are on your controller, so the solver can tell how far your hands
have to move between actions (the user: a fader right after a fader costs nothing,
but jumping from the top-left of the left deck to the bottom-right of the right one
takes time, so it's better to finish one side before moving to the other).

Positions are in CENTIMETRES from the top-left corner of the controller, measured off
Pioneer's official top-down photo of the DDJ-FLX6 (the unit is 71.1 cm wide; in that
photo 1 pixel = 0.043 cm). Close enough for "how far is it", which is all we need.

DDJ-FLX6 layout (4 channels, 2 jog wheels):
  * LEFT side controls Decks 1 / 3, RIGHT side Decks 2 / 4 (DECK button switches).
  * Each side, top to bottom: loop IN / OUT / RELOOP-EXIT, DECK button, jog wheel,
    SHIFT, CUE, PLAY; to the right of CUE/PLAY: 4 pad-mode buttons (HOT CUE, PAD FX,
    BEAT JUMP, SAMPLER; with SHIFT: KEYBOARD, KEY SHIFT, BEAT LOOP, SP. SCRATCH) above
    8 pads (2 rows of 4: hot cues A-D on top, E-H below).
  * Mixer, channel faders left to right: 3, 1, 2, 4. LOAD buttons at the top.
"""

from __future__ import annotations

from typing import Optional

PX_TO_CM = 0.043  # DDJ-FLX6 photo scale

# Left-side controls in photo pixels (x, y); the right side is the same, 1045 px further right.
_LEFT_PX = {
    "loop_in": (137, 152),
    "loop_out": (197, 152),
    "reloop": (260, 152),
    "deck_select": (136, 290),
    "jog": (385, 455),
    "shift": (137, 660),
    "cue": (158, 758),
    "play": (158, 856),
    "mode_hot_cue": (288, 760),
    "mode_pad_fx": (354, 760),
    "mode_beat_jump": (420, 760),
    "mode_sampler": (487, 760),
}
_PADS_PX = [(288, 805), (354, 805), (420, 805), (487, 805),  # A B C D
            (288, 862), (354, 862), (420, 862), (487, 862)]  # E F G H
_RIGHT_OFFSET_PX = 1045
# Mixer: channel fader x per deck (left to right 3, 1, 2, 4), and the other mixer bits
_FADER_X_PX = {3: 733, 1: 821, 2: 909, 4: 997}
_FADER_Y_PX = 735
_LOAD_Y_PX = 233
_CROSSFADER_PX = (866, 862)


def _cm(px: tuple[float, float]) -> tuple[float, float]:
    return (px[0] * PX_TO_CM, px[1] * PX_TO_CM)


PAD_MODES = {"hot_cue": "HOT CUE", "beat_jump": "BEAT JUMP", "beat_loop": "BEAT LOOP"}
# Mode button for each pad mode (BEAT LOOP = SHIFT + the BEAT JUMP button)
_MODE_BUTTON = {"hot_cue": "mode_hot_cue", "beat_jump": "mode_beat_jump", "beat_loop": "mode_beat_jump"}


class Layout:
    """Positions of every control we use, for a deck setup (decks, jog wheels)."""

    def __init__(self, decks: int = 4, jog_wheels: int = 2):
        self.decks = decks
        self.jog_wheels = jog_wheels

    # ----- which side / where -------------------------------------------------------

    def side_of(self, deck: int) -> int:
        """Jog-wheel side (1 = left) that controls `deck` (decks dealt out in turn)."""
        return (deck - 1) % self.jog_wheels + 1

    def _side_offset_px(self, side: int) -> float:
        """How far right (px) a side's controls are from the left side's."""
        if self.jog_wheels == 2:
            return _RIGHT_OFFSET_PX * (side - 1)
        # other setups: spread the sides evenly over the same width
        return _RIGHT_OFFSET_PX * (side - 1) / max(1, self.jog_wheels - 1)

    def side_control(self, deck: int, name: str) -> tuple[float, float]:
        """A deck-section control (play, cue, loop_in, mode_beat_jump...) for `deck`."""
        x, y = _LEFT_PX[name]
        return _cm((x + self._side_offset_px(self.side_of(deck)), y))

    def pad(self, deck: int, index: Optional[int] = None) -> tuple[float, float]:
        """Pad 0-7 (A-H) on `deck`'s side; None = the middle of the pads."""
        off = self._side_offset_px(self.side_of(deck))
        if index is None:
            xs = [p[0] for p in _PADS_PX]
            ys = [p[1] for p in _PADS_PX]
            return _cm((sum(xs) / len(xs) + off, sum(ys) / len(ys)))
        x, y = _PADS_PX[index]
        return _cm((x + off, y))

    def fader(self, deck: int) -> tuple[float, float]:
        """`deck`'s channel fader (FLX6 order 3 1 2 4; other decks spread beside them)."""
        if deck in _FADER_X_PX:
            return _cm((_FADER_X_PX[deck], _FADER_Y_PX))
        return _cm((_FADER_X_PX[4] + 88 * (deck - 4), _FADER_Y_PX))

    def mode_button(self, deck: int, mode: str) -> tuple[float, float]:
        return self.side_control(deck, _MODE_BUTTON[mode])

    def home(self, hand: str) -> tuple[float, float]:
        """Where a hand rests at the start: over its side's pads (left hand left side)."""
        return self.pad(1 if hand == "left" else min(2, self.decks))


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line distance in cm."""
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
