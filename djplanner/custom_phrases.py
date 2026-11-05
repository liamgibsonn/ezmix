"""
Your own ("Custom") phrase sections, made in the app's phrase editor.

Stored in custom_phrases.json in the project folder (NEVER in Rekordbox), one entry per
track, keyed by Rekordbox's track id:

    {"tracks": {
        "242576178": {
            "title": "Artist - Track Title",        # so the file is readable by eye
            "sections": [["Intro", 1, 32], ["Groove", 33, 48], ...],
            "based_on": "detected",                    # "detected" = started from Auto (EDM),
                                                       # "blank" = started from nothing
            "note": "bass still in at 41, so drop",    # optional: why (helps tuning)
            "edited_at": "2026-10-15T01:23:45"
        }}}

Sections are [name, first_bar, last_bar] with the last bar INCLUDED, the same format as
phrase_labels.json, so edited tracks can be used to score and tune the Auto (EDM)
detection (phrase_eval.py reads this file too). Gaps between sections are allowed (bars
you haven't labelled yet); overlaps are not.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from .models import Phrase

CUSTOM_FILE = config.PROJECT_DIR / "custom_phrases.json"

# Section kinds you can pick, in the order the editor lists them, with display names.
# (Same kinds and names as the Auto (EDM) detection.)
KIND_NAMES = {
    "intro": "Intro", "build": "Build", "predrop": "Pre-drop", "fakedrop": "Fake drop",
    "drop": "Drop", "groove": "Groove", "breakdown": "Breakdown", "outro": "Outro",
}
_KIND_OF_NAME = {name.lower(): kind for kind, name in KIND_NAMES.items()}

MAX_NOTE_CHARS = 2000


def kind_of(name: str) -> str:
    """'Pre-drop' (or 'predrop') -> 'predrop'. Raises ValueError for unknown names."""
    key = str(name).strip().lower()
    if key in KIND_NAMES:
        return key
    if key in _KIND_OF_NAME:
        return _KIND_OF_NAME[key]
    raise ValueError(f"Unknown section type: {name!r} (use one of: {', '.join(KIND_NAMES.values())})")


def validate(sections, bar_count: Optional[int] = None) -> list[list]:
    """
    Check and tidy a list of [name, first_bar, last_bar] sections. Returns them sorted,
    with standard names ("Pre-drop"). Raises ValueError (with a plain-words message)
    for overlaps, backwards ranges, bars before 1 or past the end of the track.
    """
    if not isinstance(sections, list):
        raise ValueError("sections must be a list")
    out = []
    for s in sections:
        if not isinstance(s, (list, tuple)) or len(s) != 3:
            raise ValueError(f"Each section must be [name, first bar, last bar], got {s!r}")
        name, first, last = s
        kind = kind_of(name)
        if isinstance(first, bool) or isinstance(last, bool) or not all(
                isinstance(x, (int, float)) and float(x).is_integer() for x in (first, last)):
            raise ValueError(f"Bars must be whole numbers, got {first!r}-{last!r}")
        first, last = int(first), int(last)
        if first < 1:
            raise ValueError(f"Sections can't start before bar 1 (got bar {first})")
        if last < first:
            raise ValueError(f"A section can't end before it starts (bars {first}-{last})")
        if bar_count is not None and last > bar_count:
            raise ValueError(f"Bar {last} is past the end of the track ({bar_count} bars)")
        out.append([KIND_NAMES[kind], first, last])
    out.sort(key=lambda s: s[1])
    for a, b in zip(out, out[1:]):
        if b[1] <= a[2]:
            raise ValueError(f"Sections overlap: {a[0]} {a[1]}-{a[2]} and {b[0]} {b[1]}-{b[2]}")
    return out


def numbered_labels(kinds: list[str], touching: Optional[list[bool]] = None) -> list[str]:
    """
    Labels with numbers where a kind appears in several separate places, the same way
    the Auto (EDM) detection names them: "Drop 1", "Drop 2"... Intro and Outro are never
    numbered, and back-to-back sections of the same kind share a label.
    touching[i] = does section i start right where section i-1 ends? (Default: yes.)
    Two drops with a gap of unlabelled bars between them are two separate drops.
    (Same rule as numberedLabels in web/phrase-editor.js.)
    """
    runs: list[list[int]] = []
    for i, k in enumerate(kinds):
        joined = touching is None or touching[i]
        if runs and kinds[runs[-1][0]] == k and joined:
            runs[-1].append(i)
        else:
            runs.append([i])
    totals: dict[str, int] = {}
    for r in runs:
        totals[kinds[r[0]]] = totals.get(kinds[r[0]], 0) + 1
    seen: dict[str, int] = {}
    labels = [""] * len(kinds)
    for r in runs:
        k = kinds[r[0]]
        seen[k] = seen.get(k, 0) + 1
        name = KIND_NAMES[k]
        base = name if totals[k] == 1 or k in ("intro", "outro") else f"{name} {seen[k]}"
        for i in r:
            labels[i] = base
    return labels


def to_phrases(sections: list[list]) -> list[Phrase]:
    """Saved sections -> Phrase objects for the rest of the app (source "custom")."""
    kinds = [kind_of(name) for name, _, _ in sections]
    touching = [i > 0 and sections[i][1] == sections[i - 1][2] + 1 for i in range(len(sections))]
    labels = numbered_labels(kinds, touching)
    return [Phrase(kind=k, label=lab, start_bar=float(first), length_bars=float(last - first + 1),
                   source="custom")
            for k, lab, (_, first, last) in zip(kinds, labels, sections)]


def from_phrases(phrases: list[Phrase]) -> list[list]:
    """Phrase objects (e.g. the Auto (EDM) detection) -> [name, first_bar, last_bar] sections."""
    out = []
    for p in phrases:
        if p.kind not in KIND_NAMES:
            continue  # (Rekordbox kinds like "chorus" don't fit; not used here)
        first = max(1, int(round(p.start_bar)))
        last = int(round(p.start_bar + p.length_bars)) - 1
        if last >= first:
            out.append([KIND_NAMES[p.kind], first, last])
    return out


# ----- the file ----------------------------------------------------------------------


def load_all(path: Path = CUSTOM_FILE) -> dict:
    """{track_id: entry} for every track with custom sections (bad/missing file = none)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tracks = data.get("tracks", {})
        return tracks if isinstance(tracks, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def get(track_id: str, path: Path = CUSTOM_FILE) -> Optional[dict]:
    """One track's entry, or None if you haven't made custom sections for it."""
    return load_all(path).get(str(track_id))


def _write(tracks: dict, path: Path) -> None:
    """Save safely: write a temporary file, then swap it in (no half-written file)."""
    body = {
        "_about": "Your own phrase sections, made in the app's phrase editor (Custom). "
                  "Sections: [name, first_bar, last_bar], last bar included. "
                  "Never written to Rekordbox.",
        "tracks": tracks,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save(track_id: str, title: str, sections, *, based_on: str = "blank", note: str = "",
         bar_count: Optional[int] = None, path: Path = CUSTOM_FILE) -> dict:
    """Validate and save one track's sections. Returns the saved entry."""
    if based_on not in ("detected", "blank"):
        raise ValueError("based_on must be 'detected' or 'blank'")
    note = str(note or "")
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"The note is too long (max {MAX_NOTE_CHARS} characters)")
    entry = {
        "title": title,
        "sections": validate(sections, bar_count),
        "based_on": based_on,
        "note": note,
        "edited_at": datetime.now().isoformat(timespec="seconds"),
    }
    tracks = load_all(path)
    tracks[str(track_id)] = entry
    _write(tracks, path)
    return entry


def delete(track_id: str, path: Path = CUSTOM_FILE) -> bool:
    """Forget a track's custom sections (back to blank). True if there were any."""
    tracks = load_all(path)
    if str(track_id) not in tracks:
        return False
    del tracks[str(track_id)]
    _write(tracks, path)
    return True
