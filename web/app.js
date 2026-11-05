// Main script: wires the library browser, the timeline and the plan controls together.
//
//   Library (Phase 1)  - track cards with phrase strips, loaded lazily as you scroll.
//   Timeline (Phase 2) - click a phrase on a strip to append it to the set, or drag it
//                        onto the timeline. See timeline.js.
//   Plans (Phase 2)    - name / save / open / new / delete. Unsaved work is also kept
//                        in the browser so a page refresh doesn't lose it.
//   Preview            - right-click a section on a strip to hear it; HOLD right-click
//                        (and drag) to jump to an exact spot. Controls appear on the
//                        track's card while it plays. Timeline blocks: double-click / ▶.
//                        See player.js.
//   Phrase editor      - "✎ Edit" on a track: change its sections on the strip (your
//                        Custom version). From Auto (EDM) it copies the detection to
//                        Custom first. See phrase-editor.js.

import * as api from "./api.js";
import { PhraseEditor } from "./phrase-editor.js";
import { PHRASE_KINDS, drawPhraseStrip, hitTest, phraseColor } from "./phrase-strip.js";
import { PreviewPlayer, trackEndBar } from "./player.js";
import {
  DRAG_NEW,
  Timeline,
  emptyPlan,
  fmtBar,
  mmss,
  segmentFromPhrase,
  segmentFromRange,
} from "./timeline.js";

// ----- page elements --------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const els = {
  status: $("library-status"),
  reload: $("reload-btn"),
  search: $("search"),
  showAll: $("show-all"),
  legend: $("legend"),
  error: $("error"),
  list: $("track-list"),
  tooltip: $("tooltip"),
  planName: $("plan-name"),
  save: $("save-btn"),
  open: $("open-select"),
  newBtn: $("new-btn"),
  del: $("delete-btn"),
  saveStatus: $("save-status"),
  sheet: $("sheet-btn"),
};

// ----- state ----------------------------------------------------------------------
let summaries = []; // track summaries from /api/tracks
const details = new Map(); // track id -> Promise of full detail JSON (cached)
let lastSavedJson = null; // plan content as last saved (to detect unsaved changes)
let phraseSource = "detected"; // "detected" (our EDM analysis), "rekordbox" or "custom"
let activeEditor = null; // the open phrase editor (one at a time), or null

// Where unsaved work is kept between page loads (this browser only).
const DRAFT_KEY = "djplanner.draft.v1";

// ----- small helpers --------------------------------------------------------------

/** Create an element with optional class and text (textContent = no HTML injection). */
function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

function showError(message) {
  els.error.hidden = !message;
  els.error.textContent = message || "";
}

/** Fetch (or reuse) a track's full detail. */
function trackDetail(id) {
  if (!details.has(id)) {
    const p = api.getTrack(id);
    p.catch(() => details.delete(id)); // don't cache failures
    details.set(id, p);
  }
  return details.get(id);
}

// ==================================================================================
// Timeline + plans
// ==================================================================================

const timeline = new Timeline({
  root: $("timeline"),
  getTrack: trackDetail,
  onPreview: (seg) => previewSegment(seg),
  onChange: () => {
    updateSaveStatus();
    saveDraft();
    scheduleCheck();
  },
});

// ----- live checking + solving -----------------------------------------------------------
// After each edit (once you pause for a moment), the plan is sent to the backend to be
// checked (decks overlapping, silence, tracks running out...) and SOLVED (how to play
// it: pads, beat jumps, loops). Problems (including the solver's "too tight" / "busy"
// ones) appear as coloured borders on blocks; the steps themselves are on the cheat sheet.

let checkTimer = null;
let checkCounter = 0; // ignore replies to older checks that arrive late

function scheduleCheck() {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, 300);
}

async function runCheck() {
  const mine = ++checkCounter;
  try {
    const res = await api.checkPlan(timeline.plan);
    if (mine === checkCounter) {
      timeline.solution = res.solution; // drawn by the setWarnings re-render below
      // The solver's own problems ("too tight" for your hands, "busy" moments) are shown
      // like the other warnings: on the block they're about.
      const actions = res.solution?.actions || [];
      const solverWarnings = (res.solution?.issues || []).map((i) => ({
        segment_id: actions[i.action_index]?.segment_id ?? null,
        level: i.level,
        message: i.message,
      }));
      timeline.setWarnings(res.warnings.concat(solverWarnings));
      // Some tracks are still having their stems analysed (about a minute each, in the
      // background): check again shortly so the vocal checks and bass-swap timing
      // catch up once they're done.
      if (res.stems_pending > 0) {
        clearTimeout(checkTimer);
        checkTimer = setTimeout(runCheck, 20000);
      }
    }
  } catch {
    /* invalid mid-edit or backend down: keep the previous warnings */
  }
}

/** The parts of a plan that count as "content" for unsaved-change detection. */
function planContent(plan) {
  return JSON.stringify({ name: plan.name, settings: plan.settings, segments: plan.segments });
}

/** Unsaved changes? A never-saved set only counts once it has blocks in it. */
function isDirty() {
  if (lastSavedJson === null) return timeline.plan.segments.length > 0;
  return planContent(timeline.plan) !== lastSavedJson;
}

function updateSaveStatus(message) {
  const dirty = isDirty();
  els.saveStatus.classList.toggle("dirty", dirty);
  els.saveStatus.textContent = message || (dirty ? "Unsaved changes" : timeline.plan.id ? "Saved" : "");
  els.del.disabled = !timeline.plan.id;
  if (els.planName.value !== timeline.plan.name) els.planName.value = timeline.plan.name;
}

/** Keep unsaved work in browser storage. Wrapped in try: storage can be blocked. */
function saveDraft() {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({ plan: timeline.plan, lastSavedJson }));
  } catch {
    /* storage unavailable: drafts just won't survive a refresh */
  }
}

function loadDraft() {
  try {
    const d = JSON.parse(localStorage.getItem(DRAFT_KEY));
    if (d && d.plan) return d;
  } catch {
    /* ignore */
  }
  return null;
}

/** Ask before throwing away unsaved changes. */
function okToDiscard() {
  return !isDirty() || !timeline.plan.segments.length ||
    confirm("You have unsaved changes to this set. Discard them?");
}

/** Show a plan in the timeline, marking it as saved. */
function showPlan(plan, warnings = [], solution = null) {
  checkCounter++; // discard any check still in flight for the previous plan
  timeline.solution = solution;
  timeline.setPlan(plan, warnings);
  lastSavedJson = plan.id ? planContent(timeline.plan) : null;
  updateSaveStatus();
  saveDraft();
}

async function refreshPlanList() {
  let plans = [];
  try {
    plans = await api.listPlans();
  } catch (e) {
    showError(`Couldn't list saved sets: ${e.message}`);
  }
  const options = [el("option", null, plans.length ? "Open…" : "No saved sets")];
  options[0].value = "";
  for (const p of plans) {
    const o = el("option", null, `${p.name} (${p.segment_count} blocks)`);
    o.value = p.id;
    o.disabled = !!p.broken;
    options.push(o);
  }
  els.open.replaceChildren(...options);
  els.open.value = "";
}

async function savePlan() {
  els.save.disabled = true;
  updateSaveStatus("Saving…");
  try {
    const plan = { ...timeline.plan, name: els.planName.value.trim() || "Untitled set" };
    let res;
    if (plan.id) {
      try {
        res = await api.updatePlan(plan.id, plan);
      } catch (e) {
        // The file was deleted elsewhere: save it as a new plan instead.
        if (!/No plan/.test(e.message)) throw e;
        res = await api.createPlan(plan);
      }
    } else {
      res = await api.createPlan(plan);
    }
    // Keep undo history: only adopt the server's id/timestamps and warnings.
    Object.assign(timeline.plan, {
      id: res.plan.id,
      name: res.plan.name,
      created_at: res.plan.created_at,
      updated_at: res.plan.updated_at,
    });
    timeline.solution = res.solution;
    timeline.setWarnings(res.warnings);
    lastSavedJson = planContent(timeline.plan);
    saveDraft();
    updateSaveStatus(`Saved ${res.plan.updated_at.slice(11, 16)}`);
    refreshPlanList();
    return true;
  } catch (e) {
    showError(`Couldn't save: ${e.message}`);
    updateSaveStatus();
    return false;
  } finally {
    els.save.disabled = false;
  }
}

async function openPlan(id) {
  if (!id || !okToDiscard()) return (els.open.value = "");
  try {
    const res = await api.getPlan(id);
    showPlan(res.plan, res.warnings, res.solution);
    showError("");
  } catch (e) {
    showError(`Couldn't open that set: ${e.message}`);
  }
  els.open.value = "";
}

function newPlan() {
  if (okToDiscard()) showPlan(emptyPlan());
}

async function deletePlan() {
  const { id, name } = timeline.plan;
  if (!id || !confirm(`Delete "${name}"?\n\nIt will be moved to the plans/.trash folder, so you can still recover it.`)) return;
  try {
    await api.deletePlan(id);
    showPlan(emptyPlan());
    refreshPlanList();
  } catch (e) {
    showError(`Couldn't delete: ${e.message}`);
  }
}

els.planName.addEventListener("input", () => timeline.setName(els.planName.value));
els.save.addEventListener("click", savePlan);
els.open.addEventListener("change", () => openPlan(els.open.value));
els.newBtn.addEventListener("click", newPlan);
els.del.addEventListener("click", deletePlan);

/** Open the cheat sheet for this set in a new tab (saving first if needed). */
async function openCheatSheet() {
  if (!timeline.plan.segments.length) return showError("Add some sections to the set first.");
  // Open the tab straight away (browsers block pop-ups opened after a delay),
  // then point it at the cheat sheet once the set is saved.
  const tab = window.open("about:blank", "_blank");
  if (isDirty() || !timeline.plan.id) {
    const ok = await savePlan();
    if (!ok) return tab?.close();
  }
  const url = `cheatsheet.html?plan=${encodeURIComponent(timeline.plan.id)}`;
  if (tab) tab.location = url;
  else window.location = url;
}
els.sheet.addEventListener("click", openCheatSheet);

// Keyboard: Ctrl+S saves; other keys go to the timeline (unless you're typing).
document.addEventListener("keydown", (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
    ev.preventDefault();
    return savePlan();
  }
  // the phrase editor gets keys first while you're working on its strip
  if (activeEditor?.handleKey(ev)) return ev.preventDefault();
  const typing = ev.target.closest("input, select, textarea, button");
  if (typing) return;
  // Space: play/pause the preview, or listen to the selected timeline block
  if (ev.key === " ") {
    ev.preventDefault();
    if (player.playing) return player.toggle();
    const seg = timeline.plan.segments.find((s) => s.id === timeline.selectedId);
    if (seg) previewSegment(seg);
    return;
  }
  if (timeline.handleKey(ev)) ev.preventDefault();
});

// Warn before closing the tab with unsaved changes (the draft is kept anyway).
window.addEventListener("beforeunload", (ev) => {
  if (isDirty() && timeline.plan.segments.length) ev.preventDefault();
  if (activeEditor?.dirty) ev.preventDefault(); // unsaved section edits
});

// ==================================================================================
// Preview player
// ==================================================================================

const player = new PreviewPlayer({ onUpdate: showPlayback, onError: (msg) => showError(msg) });
let playheadCanvases = []; // strips currently showing a playhead

/** Redraw a strip with its current hover / preview state. */
function redrawStrip(canvas) {
  const ed = canvas.editor;
  drawPhraseStrip(canvas, ed ? ed.view() : canvas.track, {
    highlight: ed ? ed.highlight() : canvas.hovered,
    emptyText: ed
      ? "Drag across the strip to add a section"
      : phraseSource === "custom" ? "No sections yet · press ✎ Edit to add some" : null,
    playRange: canvas.playRange,
    playhead: canvas.playhead,
  });
}

/** Called by the player on every frame while playing, and with null when it stops. */
function showPlayback(state) {
  // clear playheads from strips that are no longer playing
  for (const c of playheadCanvases) {
    c.playRange = c.playhead = null;
    redrawStrip(c);
  }
  playheadCanvases = [];
  // cards that aren't the playing track go back to their idle look (▶, no bar number)
  for (const ctl of document.querySelectorAll(".card-player.active")) {
    if (!state || ctl.closest(".track").dataset.id !== state.track.id) setIdle(ctl);
  }
  if (!state) return timeline.setPlaying(null);

  const { track, fromBar, toBar, bar, label, source } = state;
  const fraction = (bar - fromBar) / (toBar - fromBar);
  const whole = Math.floor(bar);
  const beat = Math.floor((bar - whole) * 4) + 1;
  const status = state.loading
    ? "Loading…"
    : state.ended
      ? `${label} · finished (▶ to replay)`
      : state.freePlay
        ? state.paused ? "" : "Playing to the end of the track"
        : `${label} (bars ${fmtBar(fromBar)}–${fmtBar(toBar - 1)})`;

  // controls + playhead on every visible card of this track
  for (const li of document.querySelectorAll(`.track[data-id="${CSS.escape(track.id)}"]`)) {
    const ctl = li.querySelector(".card-player");
    if (ctl) {
      ctl.classList.add("active");
      ctl.classList.toggle("paused", state.paused && !state.loading);
      ctl.closeBtn.hidden = !!state.freePlay; // ✕ only while previewing a section
      ctl.toggleBtn.textContent = state.paused ? "▶" : "❚❚";
      ctl.toggleBtn.title = state.paused ? "Play (Space)" : "Pause (Space)";
      ctl.barText.textContent = state.loading ? "…" : `Bar ${whole}.${beat}`;
      ctl.statusText.textContent = status;
      ctl.skipBtns.forEach((b) => (b.disabled = false));
    }
    const c = li.querySelector("canvas.strip");
    if (c?.track && !c.scrubbing) {
      c.playRange = state.freePlay ? null : [fromBar, toBar];
      c.playhead = state.loading ? null : bar;
      redrawStrip(c);
      playheadCanvases.push(c);
    }
  }
  // progress bar on the timeline block, if a block is being previewed
  // (cleared once you've jumped elsewhere: you're no longer inside that block)
  timeline.setPlaying(state.freePlay ? null : source?.segId ?? null, fraction);
}

/** Play one phrase of a track. Doing it again on the same phrase stops it. */
function previewPhrase(track, phrase) {
  const s = player.state;
  if (s && !s.freePlay && s.track.id === track.id && s.fromBar === phrase.start_bar && !s.source?.segId) {
    return player.toggle();
  }
  showError("");
  player.play(track, phrase.start_bar, phrase.end_bar, phrase.label);
}

/** Play a timeline block (the track bars it covers). Again on the same block stops it. */
async function previewSegment(seg) {
  if (player.state?.source?.segId === seg.id && !player.state.freePlay) return player.toggle();
  let track;
  try {
    track = await trackDetail(seg.track_id);
  } catch {
    return showError(`Can't preview "${seg.label}": its track isn't in your library.`);
  }
  showError("");
  player.play(track, seg.start_bar, seg.start_bar + seg.length_bars, seg.label, { segId: seg.id });
}

/** Play a track from an exact bar to its end (used when you hold right-click). */
function playFrom(track, bar) {
  showError("");
  player.play(track, bar, trackEndBar(track), `From bar ${Math.floor(bar)}`);
  if (player.state) player.state.freePlay = true;
}

/**
 * Play / pause / skip buttons for ONE track, in its card's title row.
 * They show when you hover the card, or while this track is playing OR paused: pausing
 * is how you stop, and the controls stay (dimmed, at the bar you stopped on) until
 * another track is played.
 * The ✕ only appears during a section preview ("bars x–y"): it drops the section limit
 * and carries on playing through the rest of the song.
 * Every button acts on this card's own track: ▶ on a track that isn't playing starts
 * it (and stops whatever else was playing).
 */
function cardControls(trackId) {
  const box = el("div", "card-player");
  const isMine = () => player.state?.track.id === trackId;
  const btn = (text, title, fn, cls) => {
    const b = el("button", cls, text);
    b.type = "button";
    b.title = title;
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      fn();
    });
    box.append(b);
    return b;
  };
  box.toggleBtn = btn("▶", "Play this track", async () => {
    if (isMine()) return player.toggle();
    try {
      playFrom(await trackDetail(trackId), 1); // start this track from the top
    } catch (e) {
      showError(`Couldn't play: ${e.message}`);
    }
  }, "cp-toggle");
  box.skipBtns = [
    btn("−8", "Back 8 bars", () => isMine() && player.skip(-8)),
    btn("+8", "Forward 8 bars", () => isMine() && player.skip(8)),
  ];
  box.barText = el("span", "cp-bar");
  box.statusText = el("span", "cp-text");
  box.append(box.barText, box.statusText);
  box.closeBtn = btn("✕", "Stop the section here and carry on through the whole song",
    () => isMine() && player.playOn(), "icon-btn");
  setIdle(box);
  return box;
}

/** A card's controls when its track isn't the one playing. */
function setIdle(box) {
  box.classList.remove("active", "paused");
  box.closeBtn.hidden = true;
  box.toggleBtn.textContent = "▶";
  box.toggleBtn.title = "Play this track";
  box.barText.textContent = "";
  box.statusText.textContent = "";
  box.skipBtns.forEach((b) => (b.disabled = true));
}

// ==================================================================================
// Library browser
// ==================================================================================

async function loadLibrary() {
  showError("");
  try {
    const lib = await api.getLibrary();
    if (!lib.loaded) {
      els.status.textContent = "Library not loaded";
      showError(
        `Couldn't read the Rekordbox library.\n${lib.error}\n\n` +
          `Looked in: ${lib.rekordbox_dir}\nCheck REKORDBOX_DIR in djplanner/config.py.`
      );
      els.list.replaceChildren();
      return;
    }
    const time = lib.loaded_at ? lib.loaded_at.slice(11, 16) : "";
    els.status.textContent = `${lib.track_count} tracks · read ${time}`;
    summaries = await api.getTracks(true); // fetch all; filtering happens here
    renderList();
  } catch (e) {
    showError(`Couldn't reach the planner backend (is it running?): ${e.message}`);
  }
}

/** Tracks matching the search box and filter checkbox. */
function visibleTracks() {
  const q = els.search.value.trim().toLowerCase();
  return summaries.filter((t) => {
    if (!els.showAll.checked && !t.has_beatgrid) return false;
    if (!q) return true;
    return `${t.title} ${t.artist}`.toLowerCase().includes(q);
  });
}

function renderList() {
  const tracks = visibleTracks();
  els.list.replaceChildren(...tracks.map(trackCard));
  if (!tracks.length) {
    els.list.append(el("li", "empty", summaries.length ? "No tracks match." : "No tracks found."));
  }
}

// ----- one track card -------------------------------------------------------------

function trackCard(t) {
  const li = el("li", "track");
  li.dataset.id = t.id;

  // Header row: title, artist, then BPM / key / length on the right.
  const head = el("div", "track-head");
  head.title = "Click to show phrase and cue details";
  head.append(el("span", "track-title", t.title));
  if (t.artist) head.append(el("span", "track-artist", t.artist));
  const meta = el("span", "track-meta");
  meta.append(el("span", null, t.bpm ? `${t.bpm.toFixed(t.bpm % 1 ? 2 : 0)} BPM` : "– BPM"));
  if (t.key) {
    const pill = el("span", "key-pill", t.camelot ? `${t.camelot} · ${t.key}` : t.key);
    pill.title = "Camelot code · key";
    meta.append(pill);
  }
  meta.append(el("span", null, mmss(t.duration_s)));
  if (t.has_beatgrid) head.append(cardControls(t.id));
  if (t.has_beatgrid && phraseSource !== "rekordbox") {
    const edit = el("button", "edit-btn", "✎ Edit");
    edit.type = "button";
    edit.title = phraseSource === "custom"
      ? "Edit your sections for this track"
      : "Edit this track's sections: copies the Auto (EDM) ones to Custom, ready to change";
    edit.addEventListener("click", (ev) => {
      ev.stopPropagation(); // (don't open the details)
      startEditing(t.id);
    });
    meta.prepend(edit); // on the right, just before the BPM
  }
  head.append(meta);
  li.append(head);

  // Badges: anything you should know at a glance.
  const badges = el("div", "badges");
  if (!t.has_beatgrid) badges.append(el("span", "badge bad", "No beatgrid"));
  else if (!t.has_phrases && phraseSource === "rekordbox") {
    badges.append(el("span", "badge warn", "No Rekordbox phrase analysis · 16-bar blocks"));
  }
  if (!t.file_exists) badges.append(el("span", "badge bad", "Audio file missing"));
  if (t.hot_cues_used.length) badges.append(el("span", "badge", `Hot cues: ${t.hot_cues_used.join(" ")}`));
  if (t.memory_cue_count) badges.append(el("span", "badge", `${t.memory_cue_count} memory cue(s)`));
  if (badges.childElementCount) li.append(badges);

  // Phrase strip (drawn once the detail arrives).
  if (t.has_beatgrid) {
    const canvas = el("canvas", "strip");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `Phrase strip for ${t.title}`);
    li.append(canvas);
    stripObserver.observe(li);
  }

  head.addEventListener("click", () => toggleDetails(li, t.id));
  return li;
}

// ----- lazy loading + strip interaction -------------------------------------------

// Only fetch/draw strips for cards that are on (or near) the screen.
const stripObserver = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      stripObserver.unobserve(entry.target);
      ensureStrip(entry.target);
    }
  },
  { rootMargin: "300px" }
);

// Redraw strips when their width changes (window resize etc.).
const resizeObserver = new ResizeObserver((entries) => {
  for (const entry of entries) {
    if (entry.target.track) redrawStrip(entry.target);
  }
});

/** Set up a card's strip once (the editor can ask for it before it scrolls into view). */
function ensureStrip(li) {
  if (!li.stripReady) li.stripReady = setupStrip(li);
  return li.stripReady;
}

async function setupStrip(li) {
  const canvas = li.querySelector("canvas.strip");
  let track;
  try {
    track = await trackDetail(li.dataset.id);
  } catch (e) {
    li.append(el("p", "warnings", `Couldn't load analysis: ${e.message}`));
    return;
  }
  canvas.track = track; // remembered for redraws, hover, clicks
  canvas.hovered = null; // phrase currently under the mouse
  canvas.draggable = true;
  redrawStrip(canvas);
  resizeObserver.observe(canvas);

  // While the phrase editor is open, everything works on the EDITED sections.
  const cur = () => (canvas.editor ? canvas.editor.view() : track);

  // Hover: highlight the phrase + tooltip
  canvas.addEventListener("mousemove", (ev) => {
    const hit = hitTest(canvas, cur(), ev.clientX);
    if (hit.phrase !== canvas.hovered) {
      canvas.hovered = hit.phrase;
      redrawStrip(canvas);
    }
    showTooltip(ev, hit);
  });
  canvas.addEventListener("mouseleave", () => {
    canvas.hovered = null;
    redrawStrip(canvas);
    els.tooltip.hidden = true;
  });

  // Right mouse button:
  //   quick click       -> listen to that section (again to pause)
  //   hold, or drag     -> jump to exactly that spot; while held, playback follows the
  //                        pointer, and carries on from where you let go
  canvas.addEventListener("contextmenu", (ev) => ev.preventDefault()); // no browser menu
  const HOLD_MS = 250; // how long a press counts as "holding"
  const barAtX = (x) => hitTest(canvas, cur(), x).position;
  let right = null; // state of a right-button press: {startX, x, scrubbing, lastSeek, timer}
  const jumpTo = (bar) => {
    if (player.state?.track.id === track.id) {
      player.seek(bar);
      if (player.paused) player.resume();
    } else {
      playFrom(track, bar);
    }
  };
  const startScrub = () => {
    if (!right || right.scrubbing) return;
    right.scrubbing = canvas.scrubbing = true;
    els.tooltip.hidden = true;
    jumpTo(barAtX(right.x));
    right.lastSeek = performance.now();
  };
  canvas.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 2) return;
    canvas.setPointerCapture(ev.pointerId);
    right = { startX: ev.clientX, x: ev.clientX, scrubbing: false, lastSeek: 0 };
    right.timer = setTimeout(startScrub, HOLD_MS);
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (!right) return;
    right.x = ev.clientX;
    if (!right.scrubbing && Math.abs(ev.clientX - right.startX) > 4) startScrub();
    if (!right.scrubbing) return;
    canvas.playhead = barAtX(ev.clientX); // follow the pointer straight away...
    redrawStrip(canvas);
    if (performance.now() - right.lastSeek > 120) {
      player.seek(canvas.playhead); // ...and move the audio every ~120 ms
      right.lastSeek = performance.now();
    }
  });
  canvas.addEventListener("pointerup", (ev) => {
    if (!right || ev.button !== 2) return;
    clearTimeout(right.timer);
    const wasScrubbing = right.scrubbing;
    right = null;
    canvas.scrubbing = false;
    if (wasScrubbing) return jumpTo(barAtX(ev.clientX));
    const { phrase } = hitTest(canvas, cur(), ev.clientX);
    if (phrase) previewPhrase(track, phrase);
  });
  canvas.addEventListener("pointercancel", () => {
    if (right) clearTimeout(right.timer);
    right = null;
    canvas.scrubbing = false;
  });

  // Click: add the phrase to the end of the set
  canvas.addEventListener("click", (ev) => {
    if (canvas.editor) return; // editing: clicks select sections instead
    const { phrase } = hitTest(canvas, track, ev.clientX);
    if (phrase) timeline.append(segmentFromPhrase(track, phrase));
  });

  // Drag: carry the phrase onto a deck lane at a chosen set position
  canvas.addEventListener("dragstart", (ev) => {
    if (canvas.editor) return ev.preventDefault();
    const { phrase } = hitTest(canvas, track, ev.clientX);
    if (!phrase) return ev.preventDefault();
    els.tooltip.hidden = true;
    const seg = segmentFromPhrase(track, phrase);
    timeline.startExternalDrag(seg); // so the timeline can preview its size
    ev.dataTransfer.setData(DRAG_NEW, JSON.stringify(seg));
    ev.dataTransfer.effectAllowed = "copy";
    ev.dataTransfer.setDragImage(dragChip(phrase, track), 10, 10);
  });
  canvas.addEventListener("dragend", () => timeline.endDrag());
}

/** Small coloured label used as the drag image (instead of a ghost of the whole strip). */
function dragChip(phrase, track) {
  let chip = $("drag-chip");
  if (!chip) {
    chip = el("div", "drag-chip");
    chip.id = "drag-chip";
    document.body.append(chip);
  }
  chip.textContent = `${phrase.label} · ${track.title}`;
  chip.style.background = phraseColor(phrase.kind);
  return chip;
}

/** Hover popup: just where you are and which section it is, e.g. "Bar 33, beat 2 · Drop 1 · 16 bars".
 * (How to use the strip is explained once, at the top of the track list.) */
function showTooltip(ev, { bar, beat, phrase }) {
  let text = `Bar ${bar}, beat ${beat}`;
  if (phrase) text += ` · ${phrase.label} · ${fmtBar(phrase.length_bars)} bars`;
  els.tooltip.textContent = text;
  els.tooltip.hidden = false;
  // Keep the tooltip on screen near the pointer.
  const tw = els.tooltip.offsetWidth;
  const x = Math.min(ev.clientX + 12, window.innerWidth - tw - 8);
  els.tooltip.style.left = `${Math.max(8, x)}px`;
  els.tooltip.style.top = `${ev.clientY + 16}px`;
}

// ----- expandable details -----------------------------------------------------------

async function toggleDetails(li, id) {
  const existing = li.querySelector(".details-wrap");
  if (existing) return existing.remove();

  const wrap = el("div", "details-wrap");
  li.append(wrap);
  let t;
  try {
    t = await trackDetail(id);
  } catch (e) {
    wrap.append(el("p", "warnings", e.message));
    return;
  }

  if (t.warnings.length) {
    const ul = el("ul", "warnings");
    t.warnings.forEach((w) => ul.append(el("li", null, w)));
    wrap.append(ul);
  }

  if (t.has_beatgrid) wrap.append(rangeForm(t));

  const grid = el("div", "details");

  // Phrase table
  const phraseBox = el("div");
  phraseBox.append(el("h3", null, `Phrases (${t.phrases.length}) · ${t.bar_count} bars`));
  const pt = el("table");
  pt.append(rowOf("th", ["Phrase", "Bars", "Length", "Fill from"]));
  for (const p of t.phrases) {
    const tr = rowOf("td", [
      "",
      `${fmtBar(p.start_bar)}–${fmtBar(p.end_bar - 1)}`,
      fmtBar(p.length_bars),
      p.fill_start_bar != null ? `bar ${fmtBar(p.fill_start_bar)}` : "",
    ]);
    const sw = el("span", "swatch");
    sw.style.background = phraseColor(p.kind);
    tr.firstChild.append(sw, p.label);
    pt.append(tr);
  }
  phraseBox.append(pt);
  grid.append(phraseBox);

  // Cue table
  const cueBox = el("div");
  cueBox.append(el("h3", null, `Existing cues (${t.cues.length})`));
  if (t.cues.length) {
    const ct = el("table");
    ct.append(rowOf("th", ["Pad", "Bar", "Time", "Name"]));
    for (const c of t.cues) {
      const pad = c.type === "memory" ? "Memory" : c.pad ? `Hot ${c.pad}` : `Hot ? (kind ${c.raw_kind})`;
      const loop = c.loop_end_bar != null ? ` (loop to ${c.loop_end_bar.toFixed(2)})` : "";
      ct.append(rowOf("td", [pad, c.bar.toFixed(2) + loop, mmss(c.time_s), c.comment]));
    }
    cueBox.append(ct);
  } else {
    cueBox.append(el("p", "muted", "None, so all 8 hot cue pads are free."));
  }
  grid.append(cueBox);
  wrap.append(grid);
}

/** "Add bars [from] to [to] to the set" form, for custom ranges. */
function rangeForm(t) {
  const form = el("form", "range-form");
  const from = el("input");
  const to = el("input");
  for (const inp of [from, to]) {
    Object.assign(inp, { type: "number", min: 1, max: t.bar_count, step: 1, required: true });
  }
  from.value = 1;
  to.value = Math.min(16, t.bar_count);
  const add = el("button", null, "Add to set");
  add.type = "submit";
  form.append("Add bars", from, "to", to, add);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const a = +from.value;
    const b = +to.value;
    if (b < a) return alert("The end bar must be the same as or after the start bar.");
    timeline.append(segmentFromRange(t, a, b));
  });
  return form;
}

/** Build a table row of <th> or <td> cells from an array of strings. */
function rowOf(cellTag, values) {
  const tr = el("tr");
  values.forEach((v) => tr.append(el(cellTag, null, v ?? "")));
  return tr;
}

// ----- legend + library events --------------------------------------------------------

function renderLegend() {
  els.legend.replaceChildren();
  if (phraseSource === "custom") {
    els.legend.append(el("span", "muted",
      "Your own sections: press ✎ Edit on a track to add or change them. "));
  }
  for (const [kind, label] of PHRASE_KINDS[phraseSource]) {
    const item = el("span");
    const swatch = el("i");
    swatch.style.background = phraseColor(kind);
    item.append(swatch, label);
    els.legend.append(item);
  }
}

// ----- phrase source toggle (our EDM detection vs Rekordbox) ---------------------------

const sourceRadios = [...document.querySelectorAll('input[name="phrase-source"]')];

function showPhraseSource() {
  sourceRadios.forEach((r) => (r.checked = r.value === phraseSource));
  renderLegend();
}

async function loadSettings() {
  try {
    phraseSource = (await api.getSettings()).phrase_source;
  } catch {
    /* keep the default */
  }
  showPhraseSource();
}

/**
 * Switch which sections are shown (Auto (EDM) / Rekordbox / Custom) and redraw every
 * card. Returns false if it didn't happen (unsaved edits you chose to keep, or an error).
 */
async function setPhraseSource(source) {
  if (activeEditor && !closeEditor()) return false;
  try {
    phraseSource = (await api.putSettings({ phrase_source: source })).phrase_source;
  } catch (e) {
    showError(`Couldn't change phrase source: ${e.message}`);
    return false;
  }
  showPhraseSource();
  player.stop();
  details.clear(); // strips will be re-fetched with the new phrases
  renderList();
  scheduleCheck(); // phrase alignment in the solver uses the phrases too
  return true;
}

for (const radio of sourceRadios) {
  radio.addEventListener("change", async () => {
    if (!radio.checked) return;
    if (!(await setPhraseSource(radio.value))) showPhraseSource(); // put the radio back
  });
}

// ----- phrase editor ------------------------------------------------------------------

/**
 * "✎ Edit" on a track. On Auto (EDM) it first copies the detected sections into your
 * Custom version of the track (asking first if you already have one) and switches to
 * Custom; on Custom it edits what you have (blank if nothing yet).
 */
async function startEditing(id) {
  if (activeEditor?.trackId === id) return activeEditor.li.scrollIntoView({ block: "center" });
  if (activeEditor && !closeEditor()) return;
  showError("");
  try {
    if (phraseSource === "detected") {
      const mine = await api.getCustom(id);
      const replace = !mine.sections.length || confirm(
        "You already have Custom sections for this track.\n\n" +
        "OK = replace them with the Auto (EDM) ones and edit those\n" +
        "Cancel = keep yours and edit them");
      if (replace) await api.customFromDetected(id, true);
      if (!(await setPhraseSource("custom"))) return;
    }
    const li = els.list.querySelector(`.track[data-id="${CSS.escape(id)}"]`);
    if (!li) return showError("That track isn't in the list (clear the search box?).");
    li.scrollIntoView({ block: "center" });
    await ensureStrip(li);
    const canvas = li.querySelector("canvas.strip");
    if (!canvas?.track) return;
    openEditor(li, canvas, await api.getCustom(id));
  } catch (e) {
    showError(`Couldn't open the editor: ${e.message}`);
  }
}

function openEditor(li, canvas, entry) {
  player.stop();
  canvas.draggable = false; // left-drags edit sections instead of dragging to the timeline
  canvas.hovered = null;
  li.classList.add("editing");
  const id = canvas.track.id;
  activeEditor = canvas.editor = new PhraseEditor({
    li,
    canvas,
    track: canvas.track,
    entry,
    barAt: (x) => hitTest(canvas, canvas.editor ? canvas.editor.view() : canvas.track, x).position,
    onChange: () => redrawStrip(canvas),
    onSave: (body) => api.putCustom(id, body),
    getDetected: async () => (await api.customFromDetected(id, false)).sections,
    onClose: (saved) => {
      canvas.editor = null;
      if (activeEditor?.canvas === canvas) activeEditor = null;
      li.classList.remove("editing");
      canvas.draggable = true;
      if (saved) {
        details.delete(id); // re-fetch with the saved sections
        refreshCard(id);
        scheduleCheck();
      } else {
        redrawStrip(canvas);
      }
    },
  });
}

/** Close the open editor. Asks first if there are unsaved changes; false = you said keep it. */
function closeEditor() {
  if (!activeEditor) return true;
  if (activeEditor.dirty && !confirm("You have unsaved section edits. Close the editor and lose them?")) {
    return false;
  }
  activeEditor.close(false);
  return true;
}

/** Rebuild one track's card (e.g. after saving its sections) so everything uses the new ones. */
function refreshCard(id) {
  const summary = summaries.find((t) => t.id === id);
  const old = els.list.querySelector(`.track[data-id="${CSS.escape(id)}"]`);
  if (!summary || !old) return;
  const fresh = trackCard(summary);
  old.replaceWith(fresh);
  stripObserver.unobserve(fresh);
  ensureStrip(fresh);
}

els.search.addEventListener("input", renderList);
els.showAll.addEventListener("change", renderList);
els.reload.addEventListener("click", async () => {
  els.reload.disabled = true;
  els.status.textContent = "Reading Rekordbox library…";
  try {
    await api.reloadLibrary();
    details.clear(); // analysis may have changed
    await loadLibrary();
  } catch (e) {
    showError(e.message);
  } finally {
    els.reload.disabled = false;
  }
});

// ----- start up -------------------------------------------------------------------------

showPhraseSource();

// Restore unsaved work from last time, if any; otherwise start with an empty set.
const draft = loadDraft();
if (draft) {
  timeline.setPlan(draft.plan);
  lastSavedJson = draft.lastSavedJson ?? null;
  updateSaveStatus();
  runCheck();
} else {
  showPlan(emptyPlan());
}

loadSettings().then(loadLibrary);
refreshPlanList();
