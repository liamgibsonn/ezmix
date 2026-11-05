"""
The web API (FastAPI). The browser UI in /web talks to these endpoints.

Endpoints (Phase 1):
  GET  /api/library            -> load status, track count, playlists, any load error
  POST /api/library/reload     -> re-snapshot Rekordbox's database (after changes there)
  GET  /api/tracks             -> list of track summaries (?all=true includes tracks
                                  with no beatgrid, e.g. sampler one-shots)
  GET  /api/tracks/{id}        -> full detail for one track: phrases, cues, waveform,
                                  bar start times
  GET  /api/tracks/{id}/audio  -> the track's audio, for previews (read-only; MP3s are
                                  served as a cached WAV copy so seeking is exact;
                                  supports seeking via HTTP Range requests)

Endpoints (Phase 2, set plans):
  GET    /api/plans            -> list of saved plans (id, name, updated, size)
  POST   /api/plans            -> save a NEW plan (body = plan JSON without id)
  POST   /api/plans/check      -> check a plan without saving: {plan, warnings}
  GET    /api/plans/{id}       -> {plan, warnings}
  PUT    /api/plans/{id}       -> save changes to a plan; returns {plan, warnings}
  DELETE /api/plans/{id}       -> move a plan to plans/.trash/

Endpoints (Phase 3, solver):
  Every plan response above also includes "solution": the actions, cues and issues
  worked out by the solver (djplanner/solver.py). Saved plans store their actions.
  GET    /api/plans/{id}/cheatsheet      -> cheat sheet as JSON (for cheatsheet.html)
  GET    /api/plans/{id}/cheatsheet.txt  -> cheat sheet as a plain-text download
Settings:
  GET  /api/settings            -> {phrase_source}
  PUT  /api/settings            -> change them, e.g. {"phrase_source": "rekordbox"} ("detected" / "rekordbox" / "custom")
Custom sections (the phrase editor; saved in custom_phrases.json, never in Rekordbox):
  GET    /api/tracks/{id}/custom                -> {exists, sections, note, based_on, edited_at}
  PUT    /api/tracks/{id}/custom                -> save {sections, note, based_on}
  DELETE /api/tracks/{id}/custom                -> clear them (back to blank)
  POST   /api/tracks/{id}/custom/from-detected  -> start from the Auto (EDM) detection
                                                   (?save=false: just return them)
Everything else is served from the /web folder (the UI itself).
"""

from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import config, custom_phrases, preview_audio, user_settings
from .cheatsheet import build_cheatsheet, cheatsheet_text
from .models import Plan
from .plans import PlanStore, check_plan
from .solver import solve, solve_best_decks
from .rekordbox_reader import RekordboxLibrary

# One shared library object for the whole app. Tests can replace it with a fake
# (see set_library).
_library: Optional[RekordboxLibrary] = None
# If loading failed (e.g. database not found), the message is kept here so the UI can
# show it instead of a blank page.
_load_error: Optional[str] = None


# Where plans are saved. Tests swap this for a temporary folder.
_plan_store = PlanStore()


def set_library(lib) -> None:
    """Swap in a different library object (used by tests)."""
    global _library, _load_error
    _library, _load_error = lib, None


def set_plan_store(store: PlanStore) -> None:
    """Swap in a different plan folder (used by tests)."""
    global _plan_store
    _plan_store = store


def _load_library() -> None:
    """Create the library and load it, recording any error instead of crashing."""
    global _library, _load_error
    try:
        lib = _library or RekordboxLibrary()
        lib.load()
        _library, _load_error = lib, None
    except Exception as e:
        _load_error = str(e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts: load the library (unless a test already
    # supplied one).
    if _library is None:
        _load_library()
    yield


app = FastAPI(title="DJ Set Planner", lifespan=lifespan)


@app.middleware("http")
async def always_fresh_page_files(request, call_next):
    """
    Tell the browser to check for a newer version of the page's files (HTML, JS, CSS)
    every time, so a normal refresh always shows the latest version of the app (without
    this, browsers can keep using old copies of the script files for a while).
    The check is cheap: unchanged files aren't downloaded again.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _lib() -> RekordboxLibrary:
    """The loaded library, or a clear HTTP error if it isn't available."""
    if _library is None or _load_error:
        raise HTTPException(503, f"Library not loaded: {_load_error or 'not loaded yet'}")
    return _library


# ----- endpoints ----------------------------------------------------------------------


@app.get("/api/library")
def library_status():
    if _library is None or _load_error:
        return {"loaded": False, "error": _load_error, "rekordbox_dir": str(config.REKORDBOX_DIR)}
    return {
        "loaded": True,
        "error": None,
        "rekordbox_dir": str(_library.rekordbox_dir),
        "loaded_at": _library.loaded_at.isoformat(timespec="seconds") if _library.loaded_at else None,
        "track_count": len(_library.track_ids()),
        "playlists": _library.playlists,
    }


@app.post("/api/library/reload")
def reload_library():
    _load_library()
    return library_status()


@app.get("/api/tracks")
def list_tracks(all: bool = False):
    return [s.to_dict() for s in _lib().list_tracks(include_unanalysed=all)]


@app.get("/api/tracks/{track_id}")
def get_track(track_id: str):
    try:
        return _lib().get_track(track_id).to_dict()
    except KeyError:
        raise HTTPException(404, f"No track with id {track_id}")


# Some audio types Windows/Python may not know by default.
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/aiff", ".aiff")
mimetypes.add_type("audio/aiff", ".aif")


@app.get("/api/tracks/{track_id}/audio")
def get_track_audio(track_id: str):
    """
    Stream a track's audio for in-browser previews. Only files belonging to tracks in
    your library can be served (looked up by track id, never by a path you send), and
    they're only read. Seeking works because FileResponse handles Range requests.
    """
    try:
        summary = _lib().get_track(track_id).summary
    except KeyError:
        raise HTTPException(404, f"No track with id {track_id}")
    path = Path(summary.file_path)
    if not summary.file_exists or not path.is_file():
        raise HTTPException(404, "Audio file not found. Relocate it in Rekordbox or add a PATH_REMAPS entry.")
    # MP3s are played from a WAV copy: browsers seek inaccurately in MP3s (could be a
    # beat out at 174 BPM). See preview_audio.py.
    path = preview_audio.playable_path(path)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # "no-cache" = the browser may keep it, but must check with us before reusing it
    # (so a new WAV copy, or a changed file, is always picked up).
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@app.get("/api/settings")
def get_settings():
    return user_settings.load()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    """Change in-app settings. phrase_source: "detected" (our EDM detection), "rekordbox" or "custom"."""
    try:
        saved = user_settings.save(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if _library is not None and hasattr(_library, "set_phrase_source"):
        _library.set_phrase_source(saved["phrase_source"])
    return saved


# ----- custom sections (the phrase editor) ------------------------------------------------


def _custom_file() -> Path:
    """Where custom sections are saved (the library can point elsewhere, e.g. in tests)."""
    return Path(getattr(_library, "custom_file", custom_phrases.CUSTOM_FILE))


def _track_for_custom(track_id: str):
    try:
        return _lib().get_track(track_id)
    except KeyError:
        raise HTTPException(404, f"No track with id {track_id}")


def _custom_response(entry) -> dict:
    if not entry:
        return {"exists": False, "sections": [], "note": "", "based_on": "blank", "edited_at": None}
    return {"exists": True, **entry}


@app.get("/api/tracks/{track_id}/custom")
def get_custom(track_id: str):
    """Your custom sections for a track (exists=False, no sections, if you haven't made any)."""
    _track_for_custom(track_id)
    return _custom_response(custom_phrases.get(track_id, _custom_file()))


@app.put("/api/tracks/{track_id}/custom")
def put_custom(track_id: str, body: dict = Body(...)):
    """Save your custom sections: {sections: [[name, first_bar, last_bar], ...], note, based_on}."""
    track = _track_for_custom(track_id)
    try:
        entry = custom_phrases.save(
            track_id, track.summary.title, body.get("sections", []),
            based_on=body.get("based_on", "blank"), note=body.get("note", ""),
            bar_count=track.beatgrid.bar_count or None, path=_custom_file())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _custom_response(entry)


@app.delete("/api/tracks/{track_id}/custom")
def delete_custom(track_id: str):
    """Clear your custom sections for a track (it goes back to blank)."""
    _track_for_custom(track_id)
    custom_phrases.delete(track_id, _custom_file())
    return _custom_response(None)


@app.post("/api/tracks/{track_id}/custom/from-detected")
def custom_from_detected(track_id: str, save: bool = True):
    """
    Start a custom version from the Auto (EDM) detection: copies its sections into your
    custom sections for this track (replacing any you had), ready to edit.
    ?save=false just returns them without saving (the editor's "Reset to Auto (EDM)",
    which only takes effect when you press Save).
    """
    track = _track_for_custom(track_id)
    if not hasattr(_lib(), "detected_phrases"):
        raise HTTPException(501, "Auto (EDM) detection isn't available")
    try:
        phrases = _lib().detected_phrases(track_id)
    except Exception as e:  # noqa: BLE001 - no audio file, ffmpeg error...: say why
        raise HTTPException(409, f"Couldn't run the Auto (EDM) detection on this track: {e}")
    sections = custom_phrases.from_phrases(phrases)
    if not save:
        return {"exists": False, "sections": sections, "note": "", "based_on": "detected", "edited_at": None}
    entry = custom_phrases.save(track_id, track.summary.title, sections, based_on="detected",
                                path=_custom_file())
    return _custom_response(entry)


# ----- plans --------------------------------------------------------------------------


def _get_track_or_keyerror(track_id: str):
    """Track lookup for the solver; KeyError if the library isn't available."""
    if _library is None or _load_error:
        raise KeyError(track_id)
    return _library.get_track(track_id)


def _stems_pending(plan: Plan) -> int:
    """How many of the plan's tracks are still having their stems analysed."""
    stems = getattr(_library, "stems", None)
    if stems is None:
        return 0
    return sum(1 for tid in {s.track_id for s in plan.segments}
               if stems.status(tid) in ("queued", "working"))


def _plan_response(plan: Plan, solution=None) -> dict:
    """
    A plan plus: problems found when checking it, the solver's answer for how to play
    it, and how many of its tracks are still being stem-analysed (the page checks again
    later while that's above 0, so vocal-clash warnings appear once the stems are ready).
    """
    # Layout checks always run; track checks only when the library is loaded.
    library = _library if (_library is not None and not _load_error) else None
    warnings = check_plan(plan, library)
    # (the solver also picks which physical deck each timeline lane goes on)
    solution = solution or solve_best_decks(plan, _get_track_or_keyerror)[1]
    return {"plan": plan.to_dict(), "warnings": warnings, "solution": solution.to_dict(),
            "stems_pending": _stems_pending(plan)}


def _save_solved(plan: Plan) -> dict:
    """Solve the plan, store its actions in it, save it, and return the response."""
    solution = solve_best_decks(plan, _get_track_or_keyerror)[1]
    plan.actions = [a.to_dict() for a in solution.actions]
    return _plan_response(_plan_store.save(plan), solution)


def _parse_plan(body: dict) -> Plan:
    try:
        return Plan.from_dict(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/plans")
def list_plans():
    return _plan_store.list()


@app.post("/api/plans")
def create_plan(body: dict = Body(...)):
    plan = _parse_plan(body)
    plan.id = ""  # always a new file, never overwrite an existing plan
    plan.created_at = ""
    return _save_solved(plan)


@app.post("/api/plans/check")
def check_unsaved_plan(body: dict = Body(...)):
    """Check a plan without saving it (the timeline calls this as you edit)."""
    return _plan_response(_parse_plan(body))


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: str):
    try:
        return _plan_response(_plan_store.load(plan_id))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/plans/{plan_id}")
def update_plan(plan_id: str, body: dict = Body(...)):
    plan = _parse_plan(body)
    plan.id = plan_id
    try:
        # keep the original creation time
        plan.created_at = _plan_store.load(plan_id).created_at
    except FileNotFoundError:
        raise HTTPException(404, f"No plan with id {plan_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _save_solved(plan)


def _load_plan_or_http(plan_id: str) -> Plan:
    try:
        return _plan_store.load(plan_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _cheatsheet(plan_id: str) -> dict:
    plan = _load_plan_or_http(plan_id)
    # the cheat sheet uses the PHYSICAL decks the solver picked for each lane
    played, solution = solve_best_decks(plan, _get_track_or_keyerror)
    return build_cheatsheet(played, solution, _get_track_or_keyerror)


@app.get("/api/plans/{plan_id}/cheatsheet")
def get_cheatsheet(plan_id: str):
    return _cheatsheet(plan_id)


@app.get("/api/plans/{plan_id}/cheatsheet.txt")
def get_cheatsheet_text(plan_id: str):
    sheet = _cheatsheet(plan_id)
    safe_name = "".join(ch for ch in sheet["name"] if ch.isalnum() or ch in " -_").strip() or "set"
    return PlainTextResponse(
        cheatsheet_text(sheet),
        headers={"Content-Disposition": f'attachment; filename="{safe_name} - cheat sheet.txt"'},
    )


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: str):
    try:
        dest = _plan_store.delete(plan_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"deleted": plan_id, "moved_to": str(dest)}


# The UI. Mounted last so the /api routes above take priority.
app.mount("/", StaticFiles(directory=config.PROJECT_DIR / "web", html=True), name="web")
