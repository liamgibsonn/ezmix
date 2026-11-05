// Small helpers for talking to the Python backend (djplanner/api.py).
// Every function returns parsed JSON, or throws an Error with the server's message.

async function request(method, url, body) {
  const options = { method };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const res = await fetch(url, options);
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    // FastAPI puts error messages in {"detail": "..."}
    throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  }
  return data;
}

/** Library status: {loaded, error, track_count, loaded_at, playlists, ...} */
export const getLibrary = () => request("GET", "/api/library");

/** Re-read Rekordbox's database (after you analyse/add tracks in Rekordbox). */
export const reloadLibrary = () => request("POST", "/api/library/reload");

/** Track summaries. includeAll=true also returns tracks without a beatgrid. */
export const getTracks = (includeAll = false) =>
  request("GET", `/api/tracks${includeAll ? "?all=true" : ""}`);

/** Full detail for one track: phrases, cues, waveform, bar_times, warnings. */
export const getTrack = (id) => request("GET", `/api/tracks/${encodeURIComponent(id)}`);

/** In-app settings: {phrase_source: "detected" | "rekordbox" | "custom"}. */
export const getSettings = () => request("GET", "/api/settings");
export const putSettings = (changes) => request("PUT", "/api/settings", changes);

// ----- custom sections (the phrase editor) ----------------------------------------
// Saved as [[name, first_bar, last_bar], ...] (last bar included), never in Rekordbox.

const customUrl = (id) => `/api/tracks/${encodeURIComponent(id)}/custom`;

/** Your custom sections for a track: {exists, sections, note, based_on, edited_at}. */
export const getCustom = (id) => request("GET", customUrl(id));

/** Save them: body = {sections, note, based_on}. */
export const putCustom = (id, body) => request("PUT", customUrl(id), body);

/** Clear them (the track goes back to blank in Custom). */
export const deleteCustom = (id) => request("DELETE", customUrl(id));

/** The Auto (EDM) sections as a starting point. save=true also saves them as custom. */
export const customFromDetected = (id, save = true) =>
  request("POST", `${customUrl(id)}/from-detected${save ? "" : "?save=false"}`);

// ----- plans (Phase 2) -----------------------------------------------------------

/** Saved plans: [{id, name, updated_at, segment_count, total_bars}], newest first. */
export const listPlans = () => request("GET", "/api/plans");

/** Load a plan: {plan, warnings}. */
export const getPlan = (id) => request("GET", `/api/plans/${encodeURIComponent(id)}`);

/** Save a brand-new plan (gets a new id): {plan, warnings}. */
export const createPlan = (plan) => request("POST", "/api/plans", plan);

/** Check a plan without saving it: {plan, warnings}. */
export const checkPlan = (plan) => request("POST", "/api/plans/check", plan);

/** Save changes to an existing plan: {plan, warnings}. */
export const updatePlan = (id, plan) => request("PUT", `/api/plans/${encodeURIComponent(id)}`, plan);

/** Move a plan to the plans/.trash folder. */
export const deletePlan = (id) => request("DELETE", `/api/plans/${encodeURIComponent(id)}`);
