"""
Settings you change from inside the app (as opposed to config.py, which you edit by
hand). Stored in user_settings.json in the project folder.

    phrase_source: "detected"  = our own EDM phrase detection (Intro/Build/Drop/...)
                   "rekordbox" = Rekordbox's phrase analysis (Intro/Up/Chorus/...)
                   "custom"    = your own sections, made in the phrase editor
                                 (blank for a track until you edit it; was "none")
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

SETTINGS_FILE = config.PROJECT_DIR / "user_settings.json"

DEFAULTS = {"phrase_source": "detected"}
ALLOWED = {"phrase_source": {"detected", "rekordbox", "custom"}}
# Old values that were renamed: "none" (no sections) became "custom" (your own
# sections, blank until you add some), so an old setting carries on working.
RENAMED = {"phrase_source": {"none": "custom"}}


def load(path: Path = SETTINGS_FILE) -> dict:
    """Saved settings merged over the defaults (bad/missing file = defaults)."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    out = dict(DEFAULTS)
    for key, value in saved.items():
        value = RENAMED.get(key, {}).get(value, value)
        if key in ALLOWED and value in ALLOWED[key]:
            out[key] = value
    return out


def save(changes: dict, path: Path = SETTINGS_FILE) -> dict:
    """Validate and save some settings. Raises ValueError for unknown keys/values."""
    for key, value in changes.items():
        if key not in ALLOWED:
            raise ValueError(f"Unknown setting: {key}")
        if value not in ALLOWED[key]:
            raise ValueError(f"{key} must be one of {sorted(ALLOWED[key])}")
    current = {**load(path), **changes}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return current
