"""
Reads your Rekordbox library, READ-ONLY.

How it stays safe:
  1. master.db is COPIED into our own .cache folder ("snapshot"), and only the copy
     is opened. The real database is never opened by this app, so it can't be
     modified, and Rekordbox can stay open while you use the planner.
  2. All the info we need is read from the copy in one go and the copy is closed
     straight away (no long-lived database connection).
  3. The ANLZ analysis files (beatgrid, phrases, waveform) are only read, never
     written.

To pick up changes you make in Rekordbox (new tracks, re-analysis, new cues), call
RekordboxLibrary.load() again (the "Reload library" button in the UI).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import audio_analysis, config, custom_phrases, stems, user_settings
from .analysis import (
    HOT_CUE_KIND_TO_PAD,
    build_beatgrid,
    build_cue,
    fallback_phrases,
    parse_phrases,
    waveform_per_beat,
)
from .beatgrid import Beatgrid
from .keys import to_camelot
from .models import Phrase, Track, TrackSummary

log = logging.getLogger(__name__)

# When the phrase detector's code file was last loaded (see _reload_detector_if_edited).
_detector_mtime = Path(audio_analysis.__file__).stat().st_mtime


# --------------------------------------------------------------------------------------
# Plain-data copy of one track's database row
# --------------------------------------------------------------------------------------


@dataclass
class _ContentRow:
    """What we copy out of the database for each track, before the DB is closed."""

    id: str
    title: str
    artist: str
    bpm: float
    key: str
    duration_s: float
    folder_path: str  # file location as stored in Rekordbox
    analysis_path: str  # relative path of the ANLZ .DAT file inside rekordbox/share
    # Each cue as (kind, in_ms, out_ms, comment, color)
    cues: list[tuple] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# File path helpers
# --------------------------------------------------------------------------------------


def resolve_file_path(
    stored_path: str,
    remaps: dict[str, str],
    exists: Callable[[str], bool] = os.path.exists,
) -> tuple[str, bool]:
    """
    Find where a track's audio file actually is.

    Tries the path Rekordbox has stored first, then each PATH_REMAPS entry whose
    "old" prefix matches (case-insensitive, either slash style).
    Returns (path, found). If nothing exists, returns the stored path and False.
    `exists` is swappable so tests don't need real files.
    """
    if not stored_path:
        return "", False
    norm = stored_path.replace("\\", "/")
    if exists(norm):
        return norm, True
    for old, new in remaps.items():
        old_n = old.replace("\\", "/").rstrip("/")
        if norm.lower().startswith(old_n.lower() + "/"):
            candidate = new.replace("\\", "/").rstrip("/") + norm[len(old_n):]
            if exists(candidate):
                return candidate, True
    return norm, False


# --------------------------------------------------------------------------------------
# The library
# --------------------------------------------------------------------------------------


class RekordboxLibrary:
    """
    Your Rekordbox collection, loaded read-only.

    Usage:
        lib = RekordboxLibrary()
        lib.load()                     # snapshot + read the database
        lib.list_tracks()              # summaries for the track list
        lib.get_track("123456")        # full Track with beatgrid/phrases/cues
    """

    def __init__(
        self,
        rekordbox_dir: Path = config.REKORDBOX_DIR,
        cache_dir: Path = config.CACHE_DIR,
        path_remaps: Optional[dict[str, str]] = None,
        fallback_block_bars: int = config.FALLBACK_BLOCK_BARS,
    ):
        self.rekordbox_dir = Path(rekordbox_dir)
        self.cache_dir = Path(cache_dir)
        self.path_remaps = config.PATH_REMAPS if path_remaps is None else path_remaps
        self.fallback_block_bars = fallback_block_bars

        self._rows: dict[str, _ContentRow] = {}  # track id -> database row
        self._tracks: dict[str, Track] = {}  # track id -> parsed Track (cache)
        self._lock = threading.Lock()  # web requests can arrive in parallel
        # Our own phrase detection: results per track id, and a lock per track so the
        # same track is never analysed twice at once.
        self._detected: dict[str, list[Phrase]] = {}
        self._analysis_locks: dict[str, threading.Lock] = {}
        # Which phrases get_track returns: "detected" (ours), "rekordbox" or "custom"
        self.phrase_source = user_settings.load()["phrase_source"]
        # Where your Custom sections are kept (the phrase editor saves them there)
        self.custom_file = custom_phrases.CUSTOM_FILE
        # Stem analysis (drums / bass / other / vocals per bar), done in the background
        # and saved in .cache/stems/ (see stems.py). Used by the mixing checks.
        self.stems = stems.StemAnalyser(self.cache_dir / "stems")
        self.loaded_at: Optional[datetime] = None
        self.playlists: list[dict] = []

    # ----- loading ----------------------------------------------------------------

    @property
    def master_db_path(self) -> Path:
        return self.rekordbox_dir / "master.db"

    def _snapshot_db(self) -> Path:
        """
        Copy master.db (and its -wal/-shm side files if any) into our cache folder.
        Returns the path of the copy.

        Every load gets a NEW file name (master_snapshot-<time>.db). The database
        library can keep an earlier copy's side file mapped in memory even after it's
        closed, and Windows won't let us overwrite a file in that state (WinError
        1224). Older copies are deleted when they're no longer in use; any still held
        open are left for next time.
        """
        if not self.master_db_path.exists():
            raise FileNotFoundError(
                f"Rekordbox database not found at {self.master_db_path}. "
                "Set REKORDBOX_DIR in djplanner/config.py."
            )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = self.cache_dir / f"master_snapshot-{stamp}.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.master_db_path) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(dest) + suffix))
        self._remove_old_snapshots(keep=dest)
        return dest

    def _remove_old_snapshots(self, keep: Path) -> None:
        """Delete earlier snapshot copies (our own cache files only), skipping any in use."""
        for f in self.cache_dir.glob("master_snapshot*.db*"):
            if f.name.startswith(keep.name):
                continue
            try:
                f.unlink()
            except OSError:
                pass  # still open (e.g. mapped by an earlier connection): try next time

    def load(self, attempts: int = 3) -> None:
        """
        Snapshot master.db and read every track's info from the snapshot.

        If Rekordbox is writing to the database at the exact moment we copy it, the
        copy can be inconsistent and fail to open, so we retry a few times.
        """
        # Imported here so the rest of the app (and the tests) don't need pyrekordbox.
        from pyrekordbox import Rekordbox6Database

        # pyrekordbox logs a harmless warning about a missing "masterPlaylists6.xml"
        # on every open. Only show its real errors. (Must be set AFTER importing it,
        # because pyrekordbox sets up its own logging on import.)
        logging.getLogger("pyrekordbox").setLevel(logging.ERROR)

        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            snapshot = self._snapshot_db()
            db = None
            try:
                db = Rekordbox6Database(path=snapshot, db_dir=self.rekordbox_dir)
                rows = self._read_rows(db)
                playlists = self._read_playlists(db)
                break
            except Exception as e:  # inconsistent copy, locked file, etc.
                last_error = e
                log.warning("Reading library snapshot failed (attempt %d): %s", attempt, e)
                time.sleep(0.5 * attempt)
            finally:
                if db is not None:
                    db.close()
                    # close() only ends the session; the connection pool keeps the copy
                    # open (and its -shm file mapped). Shut that down too, so the copy
                    # can be deleted next time.
                    engine = getattr(db, "engine", None)
                    if engine is not None:
                        engine.dispose()
        else:
            raise RuntimeError(f"Could not read the Rekordbox library: {last_error}")

        with self._lock:
            self._rows = rows
            self._tracks = {}  # throw away parsed tracks; analysis may have changed
            self._detected = {}  # (the disk cache notices real changes by itself)
            self.playlists = playlists
            self.loaded_at = datetime.now()
        log.info("Loaded %d tracks from Rekordbox", len(rows))

    @staticmethod
    def _read_rows(db) -> dict[str, _ContentRow]:
        """Copy the fields we need out of the database into plain _ContentRow objects."""
        rows: dict[str, _ContentRow] = {}
        for c in db.get_content().all():
            rows[str(c.ID)] = _ContentRow(
                id=str(c.ID),
                title=c.Title or Path(c.FolderPath or "").stem,
                artist=c.Artist.Name if c.Artist else "",
                bpm=(c.BPM or 0) / 100.0,  # Rekordbox stores BPM x 100
                key=c.Key.ScaleName if c.Key else "",
                duration_s=float(c.Length or 0),
                folder_path=c.FolderPath or "",
                analysis_path=c.AnalysisDataPath or "",
                cues=[
                    (q.Kind or 0, q.InMsec or 0, q.OutMsec, q.Comment, q.Color)
                    for q in c.Cues
                ],
            )
        return rows

    @staticmethod
    def _read_playlists(db) -> list[dict]:
        """
        Read playlists as {id, name, parent_id, is_folder, track_ids}. Folders have no
        tracks. "Smart" playlists are listed but their contents aren't evaluated.
        """
        out = []
        for p in db.get_playlist().all():
            is_folder = p.Attribute == 1
            track_ids = []
            if not is_folder:
                songs = sorted(p.Songs, key=lambda s: s.TrackNo or 0)
                track_ids = [str(s.ContentID) for s in songs]
            out.append(
                {
                    "id": str(p.ID),
                    "name": p.Name,
                    "parent_id": str(p.ParentID) if p.ParentID else None,
                    "is_folder": is_folder,
                    "is_smart": p.Attribute == 4,
                    "track_ids": track_ids,
                }
            )
        return out

    # ----- queries ----------------------------------------------------------------

    def track_ids(self) -> list[str]:
        return list(self._rows.keys())

    def list_tracks(self, include_unanalysed: bool = False) -> list[TrackSummary]:
        """
        Summaries for every track, sorted by artist then title. By default, tracks
        with no beatgrid (sampler one-shots, un-analysed files) are left out because
        they can't be planned with. (Doesn't run our phrase detection: that only
        happens when a track's detail is needed.)
        """
        out = []
        for tid in self.track_ids():
            s = self._base_track(tid).summary
            if s.has_beatgrid or include_unanalysed:
                out.append(s)
        return sorted(out, key=lambda s: (s.artist.lower(), s.title.lower()))

    def get_track(self, track_id: str) -> Track:
        """
        Full Track, with phrases from the current phrase source:
          "detected"  - our EDM detection (audio_analysis.py), cached on disk
          "rekordbox" - Rekordbox's own phrase analysis
          "custom"    - your own sections from the phrase editor (custom_phrases.json);
                        none at all until you make some for this track
        If detection isn't possible (no audio file, ffmpeg error...) the Rekordbox
        phrases are used and a warning is added.
        """
        base = self._base_track(track_id)
        if self.phrase_source == "custom":
            entry = custom_phrases.get(track_id, self.custom_file)
            phrases = custom_phrases.to_phrases(entry["sections"]) if entry else []
            return replace(base, phrases=phrases, phrase_source="custom")
        if self.phrase_source != "detected" or len(base.beatgrid) == 0:
            return base
        try:
            phrases = self._detected_phrases(base)
        except Exception as e:  # noqa: BLE001 - any failure: fall back, don't crash
            return replace(base, warnings=base.warnings + [
                f"Auto phrase detection failed ({e}); showing Rekordbox phrases."])
        return replace(base, phrases=phrases, phrase_source="detected")

    def _base_track(self, track_id: str) -> Track:
        """Track with Rekordbox's phrases (parsed from the ANLZ files once, then cached)."""
        with self._lock:
            cached = self._tracks.get(track_id)
            row = self._rows.get(track_id)
        if cached is not None:
            return cached
        if row is None:
            raise KeyError(f"No track with id {track_id}")
        track = self._build_track(row)
        with self._lock:
            self._tracks[track_id] = track
        return track

    # ----- our own phrase detection (cached) ------------------------------------------

    def _detection_key(self, track: Track) -> dict:
        """
        Everything the detection result depends on. If any of it changes (file edited,
        beatgrid moved, detector settings/version/code changed) the cache is ignored.
        """
        path = Path(track.summary.file_path)
        st = path.stat()
        grid = track.beatgrid
        settings_text = json.dumps(audio_analysis.SETTINGS, sort_keys=True, default=str)
        return {
            "version": audio_analysis.ANALYSIS_VERSION,
            "settings": hashlib.sha1(settings_text.encode()).hexdigest()[:12],
            "code": audio_analysis.CODE_HASH,
            "file": str(path),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "beats": len(grid),
            "first_beat": round(grid.times[0], 4),
            "last_beat": round(grid.times[-1], 4),
        }

    def _reload_detector_if_edited(self) -> None:
        """
        If audio_analysis.py has been edited since it was loaded (e.g. new phrase rules
        while tuning), load the new version and forget results from the old one, so
        refreshing the page shows the new rules without restarting the app.
        """
        global _detector_mtime
        path = Path(audio_analysis.__file__)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        with self._lock:
            if mtime == _detector_mtime:
                return
            try:
                importlib.reload(audio_analysis)  # same module object, new code
            except Exception:  # noqa: BLE001 - a half-saved edit: keep the old version for now
                log.exception("phrase detector edited but couldn't be loaded; keeping the old one")
                return
            _detector_mtime = mtime
            self._detected = {}
            log.warning("phrase detector changed: tracks will be re-analysed with the new rules")

    def _detected_phrases(self, track: Track) -> list[Phrase]:
        """Our detected phrases for a track: from memory, from disk, or computed now."""
        if not track.summary.file_exists:
            raise RuntimeError("audio file not found")
        self._reload_detector_if_edited()
        tid = track.id
        with self._lock:
            if tid in self._detected:
                return self._detected[tid]
            lock = self._analysis_locks.setdefault(tid, threading.Lock())

        with lock:  # only analyse each track once, even if asked twice at once
            with self._lock:
                if tid in self._detected:
                    return self._detected[tid]
            key = self._detection_key(track)
            cache_file = self.cache_dir / "phrases" / f"{tid}.json"
            phrases = None
            try:
                saved = json.loads(cache_file.read_text(encoding="utf-8"))
                if saved.get("key") == key:
                    phrases = [Phrase(**p) for p in saved["phrases"]]
            except (OSError, ValueError, TypeError):
                pass
            if phrases is None:
                phrases = audio_analysis.detect_phrases(Path(track.summary.file_path), track.beatgrid)
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(
                    {"key": key, "phrases": [asdict(p) for p in phrases]}, indent=1), encoding="utf-8")
            with self._lock:
                self._detected[tid] = phrases
            return phrases

    def detected_phrases(self, track_id: str) -> list[Phrase]:
        """
        The Auto (EDM) detection's phrases for a track, whatever the current phrase
        source is (used to start a Custom version from them). Raises if it can't run.
        """
        base = self._base_track(track_id)
        if len(base.beatgrid) == 0:
            raise RuntimeError("this track has no beatgrid")
        return self._detected_phrases(base)

    def stem_levels(self, track_id: str) -> Optional[dict]:
        """
        A track's stem levels {stem: [dB per bar]} if they're ready, else None (and the
        track is queued for analysis in the background; about a minute per track).
        """
        try:
            base = self._base_track(track_id)
        except KeyError:
            return None
        if not base.summary.file_exists:
            return None
        return self.stems.get(track_id, Path(base.summary.file_path), base.beatgrid)

    def set_phrase_source(self, source: str) -> None:
        if source not in ("detected", "rekordbox", "custom"):
            raise ValueError("phrase source must be 'detected', 'rekordbox' or 'custom'")
        self.phrase_source = source

    # ----- building a Track from its row + ANLZ files ----------------------------

    def _anlz_path(self, row: _ContentRow, ext: str) -> Optional[Path]:
        """
        Path of a track's ANLZ file. ext = ".DAT" (beatgrid, cues) or ".EXT"
        (phrases, detailed waveform). Returns None if the file doesn't exist.
        """
        if not row.analysis_path:
            return None
        dat = self.rekordbox_dir / "share" / row.analysis_path.strip("\\/")
        p = dat.with_suffix(ext)
        return p if p.exists() else None

    @staticmethod
    def _parse_anlz(path: Optional[Path]):
        """Parse an ANLZ file, or return None if missing/unreadable."""
        if path is None:
            return None
        from pyrekordbox.anlz import AnlzFile

        try:
            return AnlzFile.parse_file(path)
        except Exception as e:
            log.warning("Could not parse %s: %s", path, e)
            return None

    def _build_track(self, row: _ContentRow) -> Track:
        warnings: list[str] = []
        # NB: never write `if anlz_file:` - pyrekordbox's AnlzFile recurses forever
        # when tested for truthiness on Python 3.14. Always use `is not None`.
        dat = self._parse_anlz(self._anlz_path(row, ".DAT"))
        ext = self._parse_anlz(self._anlz_path(row, ".EXT"))

        # --- beatgrid (PQTZ tag in the .DAT file) ---
        grid = Beatgrid([], [])
        if dat is not None and "PQTZ" in dat.tag_types:
            grid = build_beatgrid(
                {"time": e["time"], "beat": e["beat"]}
                for e in dat.get_tag("PQTZ").content.entries
            )
        if len(grid) == 0:
            warnings.append("No beatgrid: analyse this track in Rekordbox to use it.")
        elif grid.first_downbeat_index != 0:
            warnings.append(
                f"Grid starts {grid.first_downbeat_index} beat(s) before the first "
                "downbeat (pickup). Bar 1 = first downbeat."
            )

        # --- phrases (PSSI tag in the .EXT file) ---
        phrases = []
        has_phrases = False
        if ext is not None and "PSSI" in ext.tag_types and len(grid):
            pssi = ext.get_tag("PSSI").content
            entries = [
                {"beat": e["beat"], "kind": e["kind"], "fill": e["fill"], "beat_fill": e["beat_fill"]}
                for e in pssi.entries
            ]
            phrases = parse_phrases(pssi.mood, entries, pssi.end_beat, grid)
            has_phrases = bool(phrases)
        if not has_phrases and len(grid):
            phrases = fallback_phrases(grid, self.fallback_block_bars)
            warnings.append(
                f"No phrase analysis: showing plain {self.fallback_block_bars}-bar blocks."
            )
        off_bar = [p.label for p in phrases if p.start_bar != int(p.start_bar)]
        if off_bar:
            warnings.append(f"Phrases not starting on a downbeat: {', '.join(off_bar)}")

        # --- waveform (PWV3 = detailed mono waveform, in the .EXT file) ---
        waveform = []
        if ext is not None and "PWV3" in ext.tag_types and len(grid):
            heights, _whiteness = ext.get_tag("PWV3").get()
            waveform = waveform_per_beat([int(h) for h in heights], grid)

        # --- cues (from the database rows) ---
        cues = [build_cue(k, i, o, c, col, grid) for (k, i, o, c, col) in row.cues]
        cues.sort(key=lambda q: q.time_s)
        unknown = sorted({q.raw_kind for q in cues if q.type == "hot" and q.pad is None})
        if unknown:
            warnings.append(
                f"Hot cue(s) with unrecognised pad number(s) {unknown}; treated as taken."
            )

        # --- file location ---
        file_path, exists = resolve_file_path(row.folder_path, self.path_remaps)
        if not exists:
            warnings.append(
                "Audio file not found (use Rekordbox's Relocate, or add a PATH_REMAPS "
                "entry in config.py). Planning works; preview/render won't."
            )

        summary = TrackSummary(
            id=row.id,
            title=row.title,
            artist=row.artist,
            bpm=row.bpm,
            key=row.key,
            camelot=to_camelot(row.key),
            duration_s=row.duration_s,
            file_path=file_path,
            original_path=row.folder_path,
            file_exists=exists,
            has_beatgrid=len(grid) > 0,
            has_phrases=has_phrases,
            hot_cues_used=sorted(
                {q.pad for q in cues if q.type == "hot" and q.pad is not None}
            ),
            memory_cue_count=sum(1 for q in cues if q.type == "memory"),
        )
        return Track(
            summary=summary,
            beatgrid=grid,
            phrases=phrases,
            cues=cues,
            waveform=waveform,
            warnings=warnings,
        )


# Pads in order, exposed for the UI/solver.
ALL_PADS = list(HOT_CUE_KIND_TO_PAD.values())
