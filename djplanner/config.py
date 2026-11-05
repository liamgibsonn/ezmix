"""
User-editable settings for DJ Set Planner.

Everything you might want to tweak lives here, so you never have to dig through
the rest of the code. Each setting can also be overridden with an environment
variable (handy for testing), shown next to it.
"""

import json
import os
from pathlib import Path

# --------------------------------------------------------------------------------------
# Where things live
# --------------------------------------------------------------------------------------

# Root folder of this project (the folder containing README.md).
PROJECT_DIR = Path(__file__).resolve().parent.parent

# Rekordbox's data folder. This is where master.db and the "share" folder with all the
# ANLZ analysis files live. On Windows that's %APPDATA%\Pioneer\rekordbox.
# Override with the DJPLANNER_REKORDBOX_DIR environment variable.
REKORDBOX_DIR = Path(
    os.environ.get(
        "DJPLANNER_REKORDBOX_DIR",
        Path(os.environ.get("APPDATA", Path.home())) / "Pioneer" / "rekordbox",
    )
)

# Our own scratch folder. We copy master.db in here and ONLY ever read the copy, so the
# real Rekordbox database can never be modified by this app (and it works while
# Rekordbox is open). Safe to delete at any time.
CACHE_DIR = Path(os.environ.get("DJPLANNER_CACHE_DIR", PROJECT_DIR / ".cache"))

# Where saved set plans (JSON) go. Used from Phase 2 onwards.
PLANS_DIR = PROJECT_DIR / "plans"

# --------------------------------------------------------------------------------------
# Path remapping
# --------------------------------------------------------------------------------------

# If you've moved music folders and Rekordbox still has the old location stored, add a
# mapping to path_remaps.json in the project folder:
#
#     {"D:/Old/Music": "C:/Users/<you>/Music"}
#
# i.e. {"old folder prefix": "new folder prefix"}. When a track's file isn't found at
# its stored path, we try each mapping in turn. The file is yours only: it's left out of
# git (.gitignore) because it holds your own folder names. No file = no remaps.
#
# This only affects how THIS app finds files. It never changes Rekordbox. The proper fix
# is still Rekordbox's own "Relocate" feature.
#
# Use forward slashes; matching ignores upper/lower case.
PATH_REMAPS_FILE = Path(os.environ.get("DJPLANNER_PATH_REMAPS", PROJECT_DIR / "path_remaps.json"))


def load_path_remaps(file: Path) -> dict[str, str]:
    """Read {"old prefix": "new prefix"} from `file`; empty if it doesn't exist."""
    if not file.exists():
        return {}
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ValueError(f"{file} must be a JSON object of folder -> folder strings")
    return data


PATH_REMAPS: dict[str, str] = load_path_remaps(PATH_REMAPS_FILE)

# --------------------------------------------------------------------------------------
# Library display / analysis
# --------------------------------------------------------------------------------------

# When a track has no Rekordbox phrase analysis, we chop it into plain blocks of this many
# bars (starting from bar 1) so you can still drag sections around. 16 bars is a
# standard phrase length in most dance music.
FALLBACK_BLOCK_BARS = 16

# Server address. 127.0.0.1 means only this computer can reach it. Keep it that way:
# the API exposes file paths from your library.
HOST = os.environ.get("DJPLANNER_HOST", "127.0.0.1")
PORT = int(os.environ.get("DJPLANNER_PORT", "8000"))
