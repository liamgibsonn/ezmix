"""
Audio for the in-browser previews.

Why this exists: browsers can't seek accurately in MP3 files, especially VBR ones
(variable bitrate, e.g. "ripped" tracks). To jump to a time they GUESS where it falls
in the file from a rough table, which can land a fraction of a second out: a whole
beat at 174 BPM. Rekordbox decodes properly, so a track could sound spot on there but
a beat off in this app (found with T12, 2026-10-15).

Fix: for MP3s, decode the track once with ffmpeg into a plain WAV copy in the app's
own cache folder (.cache/audio/) and play that. Browsers seek in WAV exactly. ffmpeg
is what the phrase detector uses too, and its timing lines up with Rekordbox's grid.

READ-ONLY for your music: the original file is only read. Only the cached copy is
written, and it can be deleted any time (it's just made again when needed).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from . import config
from .audio_analysis import find_ffmpeg

# File types browsers can't seek in accurately. Other types (WAV, AIFF, FLAC, M4A)
# are served as they are.
NEEDS_DECODING = {".mp3"}

AUDIO_CACHE_DIR = config.CACHE_DIR / "audio"


def cached_wav_path(path: Path) -> Path:
    """
    Where the WAV copy of `path` lives. The name includes the file's size and
    modified time, so a changed file gets a fresh copy.
    """
    st = path.stat()
    key = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return AUDIO_CACHE_DIR / f"{digest}.wav"


def playable_path(path: Path) -> Path:
    """
    The file the browser should play for `path`: the original, or (for MP3s) a WAV
    copy made on first use. Falls back to the original if ffmpeg isn't available or
    the decode fails, so previews still work, just with the old seeking problem.
    """
    if path.suffix.lower() not in NEEDS_DECODING:
        return path
    out = cached_wav_path(path)
    if out.is_file():
        return out
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return path
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Write to a temporary name first, then rename, so a half-written file (e.g. if
    # the server stops mid-way) is never served.
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.part.wav")
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path),
           "-map", "0:a:0", "-c:a", "pcm_s16le", str(tmp)]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0 or not tmp.is_file():
            return path
        os.replace(tmp, out)
        return out
    except (OSError, subprocess.TimeoutExpired):
        return path
    finally:
        tmp.unlink(missing_ok=True)
