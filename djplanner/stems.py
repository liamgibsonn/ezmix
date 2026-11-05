"""
Stem analysis: how loud each PART of a track is, bar by bar.

Demucs (an AI model) splits a track into four "stems": drums, bass, other (synths,
leads, pads...) and vocals. We keep just the loudness of each stem in every bar (dB),
which tells the mixing checks things like "this track has vocals in bars 33-64" or
"its bass only comes in at bar 17" (djplanner/mixing.py).

  * READ-ONLY for your music: ffmpeg decodes the file into memory; nothing is written
    next to it. Only the per-bar numbers are saved, in .cache/stems/<track id>.json.
  * Slow-ish: about a minute per track on this machine's CPU, so it runs in a
    BACKGROUND thread, one track at a time, and each track is only ever done once (the
    saved result is reused until the file or its beatgrid changes).
  * Optional: if PyTorch / Demucs aren't installed, everything still works, just
    without the stem-based checks.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from .audio_analysis import find_ffmpeg
from .beatgrid import Beatgrid

log = logging.getLogger(__name__)

STEMS_VERSION = 1  # bump if the measurement changes (old results are then redone)
SAMPLE_RATE = 44100  # what Demucs expects
MODEL_NAME = "htdemucs"
STEM_NAMES = ("drums", "bass", "other", "vocals")


def decode_stereo(path: Path) -> np.ndarray:
    """Decode a file to (2, samples) float32 at 44.1 kHz (READ-ONLY: output goes to a pipe)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-ac", "2", "-ar", str(SAMPLE_RATE),
           "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg couldn't decode the file: {r.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, 2).T.copy()


def per_bar_db(mono: np.ndarray, grid: Beatgrid, sr: int = SAMPLE_RATE) -> list[float]:
    """Loudness (RMS, dB) of each bar, bar 1 = first downbeat. Rounded to 0.1 dB."""
    times = grid.times
    fd = grid.first_downbeat_index
    beat_len = (times[-1] - times[0]) / max(1, len(times) - 1)
    out = []
    for b in range(grid.bar_count):
        i0 = fd + b * 4
        t0 = times[i0]
        t1 = times[i0 + 4] if i0 + 4 < len(times) else t0 + 4 * beat_len
        seg = mono[int(t0 * sr):int(t1 * sr)]
        out.append(round(float(10 * np.log10(np.mean(seg ** 2) + 1e-12)), 1) if len(seg) else -120.0)
    return out


class StemSeparator:
    """Loads the Demucs model once and splits tracks. Import errors surface on first use."""

    def __init__(self):
        self._model = None

    def _load(self):
        if self._model is None:
            import torch  # noqa: F401  (heavy: only imported when stems are first needed)
            from demucs.pretrained import get_model
            self._model = get_model(MODEL_NAME)
            self._model.eval()
        return self._model

    def levels(self, path: Path, grid: Beatgrid) -> dict[str, list[float]]:
        """{stem: [dB per bar]} for one track."""
        import torch
        from demucs.apply import apply_model

        model = self._load()
        wav = torch.from_numpy(decode_stereo(path))
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        with torch.no_grad():
            sources = apply_model(model, ((wav - mean) / std)[None], device="cpu", split=True,
                                  overlap=0.25, progress=False)[0]
        sources = sources * std + mean
        return {name: per_bar_db(sources[k].mean(0).numpy(), grid)
                for k, name in enumerate(model.sources)}


def available() -> bool:
    """Are PyTorch and Demucs installed?"""
    try:
        import demucs  # noqa: F401
        import torch  # noqa: F401
    except Exception:  # noqa: BLE001 - any import problem = not available
        return False
    return True


class StemAnalyser:
    """
    Background stem analysis with a disk cache. get() never waits: it returns the
    levels if they're ready, otherwise None (and queues the track to be done).
    """

    def __init__(self, cache_dir: Path, separator: Optional[StemSeparator] = None):
        self.cache_dir = Path(cache_dir)
        self.separator = separator or StemSeparator()
        self._ready: dict[str, dict] = {}  # track id -> levels (in memory)
        self._status: dict[str, str] = {}  # track id -> "queued" | "working" | "failed: ..."
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self.enabled = available()

    # ----- the cache file --------------------------------------------------------

    @staticmethod
    def key(path: Path, grid: Beatgrid) -> dict:
        """What a saved result depends on: the file, its beatgrid, and this code's version."""
        st = path.stat()
        return {"version": STEMS_VERSION, "model": MODEL_NAME, "file": str(path), "size": st.st_size,
                "mtime": int(st.st_mtime), "beats": len(grid),
                "first_beat": round(grid.times[0], 4), "last_beat": round(grid.times[-1], 4)}

    def _file(self, track_id: str) -> Path:
        return self.cache_dir / f"{track_id}.json"

    def _load_saved(self, track_id: str, key: dict) -> Optional[dict]:
        try:
            saved = json.loads(self._file(track_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return saved.get("levels") if saved.get("key") == key else None

    def save(self, track_id: str, key: dict, levels: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._file(track_id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"key": key, "levels": levels}), encoding="utf-8")
        tmp.replace(self._file(track_id))

    # ----- asking for a track ------------------------------------------------------

    def get(self, track_id: str, path: Path, grid: Beatgrid) -> Optional[dict]:
        """Stem levels if ready (memory or disk); otherwise queue the track and return None."""
        with self._lock:
            if track_id in self._ready:
                return self._ready[track_id]
        if len(grid) == 0 or not path.is_file():
            return None
        try:
            key = self.key(path, grid)
        except OSError:
            return None
        levels = self._load_saved(track_id, key)
        if levels is not None:
            with self._lock:
                self._ready[track_id] = levels
            return levels
        if self.enabled:
            with self._lock:
                if track_id not in self._status:
                    self._status[track_id] = "queued"
                    self._queue.put((track_id, path, grid, key))
                    self._start_worker()
        return None

    def status(self, track_id: str) -> str:
        """"ready", "queued", "working", "failed: ...", "unavailable" or "not requested"."""
        with self._lock:
            if track_id in self._ready:
                return "ready"
            if not self.enabled:
                return "unavailable"
            return self._status.get(track_id, "not requested")

    def pending(self) -> int:
        """How many tracks are queued or being worked on."""
        with self._lock:
            return sum(1 for s in self._status.values() if s in ("queued", "working"))

    def forget_memory(self) -> None:
        """Drop the in-memory results (e.g. after the library is re-read)."""
        with self._lock:
            self._ready.clear()

    # ----- the background worker ----------------------------------------------------

    def _start_worker(self) -> None:
        # (called with the lock held)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="stem-analysis", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                track_id, path, grid, key = self._queue.get(timeout=30)
            except queue.Empty:
                return  # idle: the thread ends; a new one starts when needed
            with self._lock:
                self._status[track_id] = "working"
            try:
                levels = self.separator.levels(path, grid)
                self.save(track_id, key, levels)
                with self._lock:
                    self._ready[track_id] = levels
                    self._status.pop(track_id, None)
            except Exception as e:  # noqa: BLE001 - one bad file mustn't stop the rest
                log.exception("stem analysis failed for %s", path)
                with self._lock:
                    self._status[track_id] = f"failed: {e}"[:200]
