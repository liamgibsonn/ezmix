"""
Core data model shared by the reader, the API and (later) the solver.

These are plain dataclasses with no Rekordbox or web code in them, so the solver can
be tested with hand-made fake tracks.

Positions are measured in BARS throughout the app:
  * Bar 1 starts at the track's first downbeat (beat "1" in the Rekordbox grid).
  * Positions are floats: 33.0 = the start of bar 33; 32.75 = beat 4 of bar 32
    (Rekordbox grids are always 4/4, so each beat is 0.25 of a bar).
  * Anything before the first downbeat has a position below 1.0 (e.g. a pickup).
Seconds are only used at the edges (reading cues, playing audio), converted via the
track's Beatgrid.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from .beatgrid import Beatgrid


@dataclass
class Phrase:
    """One section of a track (Intro, Up, Chorus, ...)."""

    # Normalised category used for colouring and by the solver:
    #   Rekordbox: "intro", "up", "down", "chorus", "verse", "bridge", "outro"
    #   our EDM detection: "intro", "build", "drop", "breakdown", "outro"
    #   "bars" for the plain fallback blocks on tracks without any phrase analysis.
    kind: str
    # Human-readable name shown in the UI, e.g. "Chorus" or "Verse 2" or "Bars 17-32".
    label: str
    # Where the phrase starts, in bars (see module docstring), and how long it is.
    start_bar: float
    length_bars: float
    # Where it came from: "rekordbox" (Rekordbox's phrase analysis), "detected" (our own
    # EDM analysis, audio_analysis.py) or "auto" (plain fallback blocks).
    source: str = "rekordbox"
    # Rekordbox marks some phrases as ending in a drum fill. If so, this is the bar
    # where the fill starts (useful later for timing transitions). None = no fill.
    fill_start_bar: Optional[float] = None

    @property
    def end_bar(self) -> float:
        """Bar position just after the phrase ends (= start of the next phrase)."""
        return self.start_bar + self.length_bars


@dataclass
class Cue:
    """An existing hot cue or memory cue on a track. We never change or delete these."""

    # "hot" (on a performance pad) or "memory" (marker on the waveform, no pad).
    type: str
    # Pad letter "A".."H" for hot cues; None for memory cues, or for a hot cue with a
    # pad number we don't recognise. The solver treats unknown pads as "taken".
    pad: Optional[str]
    # Raw Rekordbox "Kind" number, kept so we can debug the pad mapping.
    raw_kind: int
    # Position of the cue, in seconds and in bars.
    time_s: float
    bar: float
    # If this cue is a saved loop: where the loop ends. None for a plain cue.
    loop_end_s: Optional[float] = None
    loop_end_bar: Optional[float] = None
    # The cue's name and colour as set in Rekordbox ("" / None if not set).
    comment: str = ""
    color: Optional[int] = None


@dataclass
class TrackSummary:
    """The lightweight info shown in the track list (no heavy analysis data)."""

    id: str
    title: str
    artist: str
    bpm: float  # average BPM from Rekordbox; 0 if not analysed
    key: str  # musical key as Rekordbox shows it, e.g. "Abm"; "" if unknown
    camelot: str  # same key on the Camelot wheel, e.g. "1A"; "" if unknown
    duration_s: float
    file_path: str  # where we actually found the file (after path remapping)
    original_path: str  # where Rekordbox thinks the file is
    file_exists: bool
    has_beatgrid: bool
    has_phrases: bool  # True = real Rekordbox phrase analysis is available
    hot_cues_used: list[str] = field(default_factory=list)  # pad letters in use
    memory_cue_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Track:
    """Everything we know about one track, ready for display or for the solver."""

    summary: TrackSummary
    beatgrid: Beatgrid
    phrases: list[Phrase]
    cues: list[Cue]
    # Loudness of the track per beat (0.0-1.0), one value for each beat in the grid.
    # Drawn as a mini waveform aligned to bars. Empty if no waveform data was found.
    waveform: list[float] = field(default_factory=list)
    # Anything odd we noticed while loading (shown in the UI as small warnings).
    warnings: list[str] = field(default_factory=list)
    # Where `phrases` came from: "rekordbox" (Rekordbox's analysis, or plain 16-bar
    # blocks if it has none), "detected" (our own EDM detection) or "none" (no phrases).
    phrase_source: str = "rekordbox"

    @property
    def id(self) -> str:
        return self.summary.id

    def to_dict(self) -> dict:
        """JSON-friendly version of the track for the web API."""
        grid = self.beatgrid
        return {
            **self.summary.to_dict(),
            "bar_count": grid.bar_count,
            # Bar position where the grid starts (usually 1.0, or a bit below it if the
            # track has beats before the first downbeat).
            "first_bar": grid.beat_index_to_bar(0) if len(grid) else 1.0,
            # Start time (seconds) of every bar 1..bar_count. Lets the browser convert
            # bars to seconds without re-implementing the grid logic.
            "bar_times": [grid.bar_to_time(b) for b in range(1, grid.bar_count + 2)]
            if len(grid)
            else [],
            "phrases": [{**asdict(p), "end_bar": p.end_bar} for p in self.phrases],
            "cues": [asdict(c) for c in self.cues],
            "waveform": [round(v, 3) for v in self.waveform],
            "warnings": self.warnings,
            "phrase_source": self.phrase_source,
        }


# --------------------------------------------------------------------------------------
# Plans (Phase 2): what you build on the timeline
# --------------------------------------------------------------------------------------
#
# The timeline has one lane per deck. Each block (Segment) sits on a deck at a position
# in the SET:
#
#   set bar    1       17      33      49
#   Deck 1   [Intro ][Chorus 1      ][Outro ]
#   Deck 2                    [Intro      ][Up]...
#                             |<- mix ->|
#
# Two kinds of bar positions, both counted from bar 1:
#   * set_bar   - where the block starts in the SET (the whole performance)
#   * start_bar - which part of the TRACK it plays (track bar positions, see top of file)
# Blocks on different decks may overlap: that's a mix. Blocks on the SAME deck may not.
# You decide WHAT plays and roughly where the mixes are; the solver (Phase 3/4) works
# out HOW: hot cues, loops to stretch a section, beat jumps, fades.

# Bump this if the plan JSON layout ever changes, so old files can be upgraded.
#   1 = single sequence of blocks (no decks / set positions)
#   2 = deck lanes: every block has `deck` and `set_bar`
PLAN_FORMAT_VERSION = 2


def _snap_to_beat(bars: float) -> float:
    """Round a bar position/length to the nearest beat (0.25 bar)."""
    return round(bars * 4) / 4


@dataclass
class Segment:
    """
    One block on the timeline: a stretch of one track, played on one deck, starting at
    a given point in the set. Usually a whole phrase, but any bar range is allowed
    ("custom").
    """

    id: str  # unique within the plan (made by the browser)
    track_id: str  # Rekordbox track ID
    start_bar: float  # where in the TRACK this block starts (track bar position)
    length_bars: float  # how long it plays for
    label: str  # name shown on the block, e.g. "Chorus 2"
    deck: int = 1  # which timeline LANE it's on (1 to MAX_LANES); the solver picks the deck
    set_bar: float = 1.0  # where in the SET it starts
    kind: str = "bars"  # phrase category, for colour (see Phrase.kind)
    source: str = "phrase"  # "phrase" = a whole phrase, "custom" = hand-picked range
    # Copied from the track when the block was added, so a saved plan is readable on its
    # own (and still shows something if the track later leaves the library).
    track_title: str = ""
    track_artist: str = ""
    bpm: float = 0.0

    @property
    def end_bar(self) -> float:
        """Track bar position just after this block finishes."""
        return self.start_bar + self.length_bars

    @property
    def set_end_bar(self) -> float:
        """Set bar position just after this block finishes."""
        return self.set_bar + self.length_bars

    def overlap_bars(self, other: "Segment") -> float:
        """How many bars this block and `other` play at the same time (0 if none)."""
        return max(0.0, min(self.set_end_bar, other.set_end_bar) - max(self.set_bar, other.set_bar))

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        """Build from JSON, checking the essentials. Raises ValueError if invalid."""
        try:
            seg = cls(
                id=str(d["id"]),
                track_id=str(d["track_id"]),
                start_bar=_snap_to_beat(float(d["start_bar"])),
                length_bars=_snap_to_beat(float(d["length_bars"])),
                label=str(d.get("label") or ""),
                deck=int(d["deck"]) if d.get("deck") is not None else 1,
                set_bar=_snap_to_beat(float(d["set_bar"])) if d.get("set_bar") is not None else 1.0,
                kind=str(d.get("kind") or "bars"),
                source=str(d.get("source") or "phrase"),
                track_title=str(d.get("track_title") or ""),
                track_artist=str(d.get("track_artist") or ""),
                bpm=float(d.get("bpm") or 0.0),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Invalid segment {d!r}: {e}") from None
        if seg.length_bars <= 0:
            raise ValueError(f"Segment '{seg.label}' must be at least one beat long")
        if not 1 <= seg.deck <= MAX_LANES:
            raise ValueError(f"Segment '{seg.label}': lane must be 1-{MAX_LANES}")
        if seg.set_bar < 1:
            raise ValueError(f"Segment '{seg.label}': set position must be bar 1 or later")
        return seg

    def to_dict(self) -> dict:
        return {**asdict(self), "end_bar": self.end_bar, "set_end_bar": self.set_end_bar}


# Most decks a set can use (some DJs play on 6).
MAX_DECKS = 6
# Most timeline lanes. Lanes aren't decks: several lanes can share a deck one after
# the other (lanes.py), and the timeline always shows one empty lane to add to.
MAX_LANES = 16


@dataclass
class PlanSettings:
    """
    Performance setup the solver must respect.

    `decks` is how many tracks you can have playing at once (e.g. 3 for three CDJs).
    `jog_wheels` is how many sets of deck controls (jog wheel, pads, PLAY/CUE, loop and
    beat-jump buttons) you have. With fewer jog wheels than decks, decks SHARE one, and
    you press a deck-select button to switch it between them. Like a DDJ-FLX6-GT (4
    decks, 2 jog wheels): Decks 1 and 3 share the left one, 2 and 4 the right one.
    Channel faders and EQs are separate for every deck, so they never need switching.
    See jog_of() for how decks are shared out.
    """

    decks: int = 2  # 1 to MAX_DECKS
    jog_wheels: int = 2  # 1 to MAX_DECKS; as many as decks or more = one per deck (nothing to switch)
    instant_doubles: bool = False  # can the same track be loaded on two decks?
    # PRE-ROLL: start each track this many bars before it comes in, with its fader DOWN,
    # so the moment it comes in is just "fader up" (the user: standard practice, you
    # know it's already in time). 0 = off (press PLAY / a pad right on the bar).
    preroll_bars: int = 4

    @property
    def shared_jogs(self) -> bool:
        """True if some decks share a jog wheel (so the cheat sheet needs switch steps)."""
        return self.jog_wheels < self.decks

    def jog_of(self, deck: int) -> int:
        """
        Which jog wheel (1 = leftmost) controls `deck`. Decks are dealt out in turn, like
        cards: with 2 jog wheels, Decks 1, 3, 5 are on jog 1 and 2, 4, 6 on jog 2; with
        3 jog wheels, 1+4, 2+5, 3+6. That matches how 4-deck controllers with 2 jog
        wheels are laid out (1/3 left, 2/4 right).
        """
        return (deck - 1) % self.jog_wheels + 1

    def jog_name(self, jog: int) -> str:
        """How a jog wheel is called in instructions: "Left"/"Right" if there are 2."""
        if self.jog_wheels == 2:
            return "Left" if jog == 1 else "Right"
        return f"Jog {jog}"

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PlanSettings":
        d = d or {}
        decks = int(d.get("decks", 2))
        if not 1 <= decks <= MAX_DECKS:
            raise ValueError(f"decks must be 1-{MAX_DECKS}")
        # Plans saved before this setting existed had one set of controls per deck.
        jogs = int(d.get("jog_wheels") or decks)
        if not 1 <= jogs <= MAX_DECKS:
            raise ValueError(f"jog_wheels must be 1-{MAX_DECKS}")
        preroll = int(d.get("preroll_bars", 4))
        if not 0 <= preroll <= 16:
            raise ValueError("preroll_bars must be 0-16")
        return cls(decks=decks, jog_wheels=jogs, instant_doubles=bool(d.get("instant_doubles", False)),
                   preroll_bars=preroll)


def _upgrade_v1_segments(raw_segments: list[dict]) -> list[dict]:
    """
    Version 1 plans were a single sequence with no decks. Put those blocks back to back
    on Deck 1, in their saved order, so old plans still open.
    """
    out, cursor = [], 1.0
    for raw in raw_segments:
        out.append({**raw, "deck": 1, "set_bar": cursor})
        cursor += float(raw.get("length_bars") or 0)
    return out


@dataclass
class Plan:
    """A whole set: the blocks on each deck, plus (from Phase 3) the solved actions."""

    id: str
    name: str
    segments: list[Segment] = field(default_factory=list)  # sorted by set position
    settings: PlanSettings = field(default_factory=PlanSettings)
    # Solver output (Phase 3+). Stored as plain dicts until the Action model exists.
    actions: list[dict] = field(default_factory=list)
    created_at: str = ""  # ISO timestamps, set by the plan store
    updated_at: str = ""
    version: int = PLAN_FORMAT_VERSION

    @property
    def total_bars(self) -> float:
        """Length of the whole set in bars (from bar 1 to the end of the last block)."""
        return max((s.set_end_bar for s in self.segments), default=1.0) - 1.0

    def deck_segments(self, deck: int) -> list[Segment]:
        """Blocks on one deck, in playing order."""
        return [s for s in self.segments if s.deck == deck]

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        """Build from JSON (upgrading old versions). Raises ValueError if invalid."""
        if not isinstance(d, dict):
            raise ValueError("Plan must be a JSON object")
        raw = list(d.get("segments") or [])
        if int(d.get("version") or PLAN_FORMAT_VERSION) < 2:
            raw = _upgrade_v1_segments(raw)
        segments = [Segment.from_dict(s) for s in raw]
        segments.sort(key=lambda s: (s.set_bar, s.deck))
        ids = [s.id for s in segments]
        if len(ids) != len(set(ids)):
            raise ValueError("Segment ids must be unique within a plan")
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or "Untitled set").strip() or "Untitled set",
            segments=segments,
            settings=PlanSettings.from_dict(d.get("settings")),
            actions=list(d.get("actions") or []),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            version=PLAN_FORMAT_VERSION,  # always saved in the current format
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "settings": asdict(self.settings),
            "segments": [s.to_dict() for s in self.segments],
            "actions": self.actions,
        }


# --------------------------------------------------------------------------------------
# Solver output (Phase 3): what you physically do, and the cues to prepare
# --------------------------------------------------------------------------------------


@dataclass
class PlannedCue:
    """A cue the plan needs on a track: one of yours (reused) or a new one to set up."""

    track_id: str
    type: str  # "hot" (on a pad) or "memory"
    track_bar: float  # where in the track
    name: str  # e.g. "→ Chorus 2" (used as the cue name in the Rekordbox XML export)
    pad: Optional[str] = None  # pad letter for hot cues
    existing: bool = False  # True = already in Rekordbox (we only reuse it, never change it)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Action:
    """
    One thing the DJ does, at a moment in the set.

    kind is one of:
      "load"        load a track onto a deck (any time before `set_bar`)
      "start"       start the deck (press PLAY from a memory cue, or hit a pad)
      "hot_cue"     hit a hot cue pad to jump
      "beat_jump"   press beat jump (params: beats, presses)
      "loop_in"     start a loop (params: loop_bars, method "auto" or "manual", passes)
      "loop_out"    set the loop end (manual loops only)
      "loop_exit"   leave the loop during its last pass
      "stop"        stop the deck / pull its fader down
      "render"      this part can't be played live; render it as an audio edit
      "fader_up"    bring a deck's channel fader up (it's been running muted: pre-roll)
      "fader_down"  pull a deck's fader down but leave it PLAYING (muted), to bring it
                    back later in time
    params["window"] = [earliest, latest] set bar: the action can be done any time in
    that window (e.g. a beat jump while the deck is muted); set_bar is the moment the
    solver picked, as far as it could from other actions.
    """

    kind: str
    deck: int
    track_id: str
    set_bar: float  # when in the SET to do it (bar position)
    text: str  # human instruction for the cheat sheet
    track_bar: Optional[float] = None  # where the deck should be/land in the TRACK
    pad: Optional[str] = None
    segment_id: Optional[str] = None  # the block this action leads into / belongs to
    params: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)  # e.g. ["awkward"]
    cost: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)
