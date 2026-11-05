"""
Musical key helpers: convert Rekordbox key names (e.g. "Abm", "F#") to the Camelot
wheel (e.g. "1A", "2B"). The Camelot wheel makes harmonic mixing easy: keys with the
same number, or numbers next to each other, mix well. The solver will use this in
Phase 4 for key-clash warnings.
"""

from __future__ import annotations

import re

# Camelot code for every key. "A" = minor keys (inner ring), "B" = major (outer ring).
# Enharmonic spellings (G# = Ab, etc.) are listed separately so either spelling works.
_CAMELOT: dict[str, str] = {
    # minor keys
    "Abm": "1A", "G#m": "1A",
    "Ebm": "2A", "D#m": "2A",
    "Bbm": "3A", "A#m": "3A",
    "Fm": "4A",
    "Cm": "5A",
    "Gm": "6A",
    "Dm": "7A",
    "Am": "8A",
    "Em": "9A",
    "Bm": "10A", "Cbm": "10A",
    "F#m": "11A", "Gbm": "11A",
    "Dbm": "12A", "C#m": "12A",
    # major keys
    "B": "1B", "Cb": "1B",
    "F#": "2B", "Gb": "2B",
    "Db": "3B", "C#": "3B",
    "Ab": "4B", "G#": "4B",
    "Eb": "5B", "D#": "5B",
    "Bb": "6B", "A#": "6B",
    "F": "7B", "E#": "7B",
    "C": "8B", "B#": "8B",
    "G": "9B",
    "D": "10B",
    "A": "11B",
    "E": "12B", "Fb": "12B",
}

# Matches a key that is already in Camelot form, like "8A" or "12b".
_CAMELOT_RE = re.compile(r"^\s*(1[0-2]|[1-9])\s*([ABab])\s*$")


def to_camelot(key_name: str | None) -> str:
    """
    Convert a key name to Camelot. Returns "" if the key is unknown or empty.
    Accepts "Abm", "Ab minor", "A flat minor"-ish variations loosely, and keys that are
    already Camelot ("8A").
    """
    if not key_name:
        return ""
    m = _CAMELOT_RE.match(key_name)
    if m:  # already Camelot
        return f"{int(m.group(1))}{m.group(2).upper()}"

    # Normalise: strip spaces, unicode sharps/flats, and long-form "minor"/"major".
    k = key_name.strip().replace("♯", "#").replace("♭", "b").replace(" ", "")
    k = re.sub(r"(?i)minor$|min$", "m", k)
    k = re.sub(r"(?i)major$|maj$", "", k)
    if not k:
        return ""
    k = k[0].upper() + k[1:]  # note letter is upper case, "b"/"m" stay lower case
    return _CAMELOT.get(k, "")
