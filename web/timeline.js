// The set timeline: one lane per deck, blocks placed at positions in the set.
//
//   Set bar   1       9       17      25      33
//   Deck 1  [Intro  ][Chorus 1        ][Outro  ]
//   Deck 2                    [Intro          ][Up]
//   Mixes                     [ 8-bar mix  ]
//
// * A block = a stretch of one track (a phrase, or a custom bar range).
// * Blocks on the SAME deck can't overlap (a deck plays one thing at a time).
// * Blocks on DIFFERENT decks may overlap: that's a mix, shown in the "Mixes" row.
// * You decide WHAT plays and where the mixes are; the solver (Phase 3/4, Python)
//   works out HOW: pre-rolls, hot cues, loops, beat jumps. Its steps go on the cheat
//   sheet; its problems show on the blocks like the other warnings (app.js).
//
// The Timeline class owns the plan being edited (name, settings, segments) and
// redraws itself after every change. app.js wires it to the rest of the page.
// Problem-checking happens in Python (/api/plans/check); this file only displays the
// results (setWarnings).

import { phraseColor } from "./phrase-strip.js";

/** Drag-and-drop data type for phrases dragged in from the library strips. */
export const DRAG_NEW = "application/x-djplanner-new-segment";
/** Data type for blocks being moved around the timeline. */
export const DRAG_MOVE = "application/x-djplanner-move-segment";

const MAX_HISTORY = 200; // undo steps kept
const MIN_BLOCK_PX = 8; // tiny blocks still get something clickable
const SNAP_MAGNET_PX = 10; // block edges "stick" to other blocks' edges within this
const EXTRA_BARS = 48; // empty room after the last block, to drop new ones into
const SNAP_OPTIONS = [
  [0.25, "Beat"],
  [1, "1 bar"],
  [4, "4 bars"],
  [8, "8 bars"],
  [16, "16 bars"],
];

// ----- helpers -----------------------------------------------------------------------

/** Random id for a new segment. (crypto.randomUUID only exists on secure pages.) */
export function uid() {
  return (crypto.randomUUID?.() || `${Date.now()}${Math.random()}`).replace(/[^a-z0-9]/gi, "").slice(0, 12);
}

/** A fresh, empty plan. */
export function emptyPlan() {
  return {
    id: null,
    name: "Untitled set",
    settings: { decks: 2, jog_wheels: 2, instant_doubles: false, preroll_bars: 4 },
    segments: [],
  };
}

/** Make a segment from a whole phrase of a track (track = /api/tracks/{id} JSON).
 *  Deck and set position are filled in when it's placed on the timeline. */
export function segmentFromPhrase(track, phrase) {
  return {
    id: uid(),
    track_id: track.id,
    start_bar: phrase.start_bar,
    length_bars: phrase.length_bars,
    label: phrase.label,
    kind: phrase.kind,
    source: "phrase",
    track_title: track.title,
    track_artist: track.artist,
    bpm: track.bpm,
    deck: 1,
    set_bar: 1,
  };
}

/** Make a custom segment covering TRACK bars `from`..`to` (inclusive). */
export function segmentFromRange(track, from, to) {
  // Colour it like the phrase it starts in.
  const p = track.phrases.find((ph) => from >= ph.start_bar && from < ph.end_bar);
  return {
    ...segmentFromPhrase(track, { start_bar: from, length_bars: to - from + 1, label: `Bars ${from}–${to}`, kind: p ? p.kind : "bars" }),
    source: "custom",
  };
}

/** Set bar just after a segment ends. */
const setEnd = (s) => s.set_bar + s.length_bars;

/** Bars two segments play at the same time (0 if they don't overlap). */
const overlap = (a, b) => Math.max(0, Math.min(setEnd(a), setEnd(b)) - Math.max(a.set_bar, b.set_bar));

/** Round to the nearest beat (0.25 bar). */
const toBeat = (x) => Math.round(x * 4) / 4;

/** 245.3 -> "4:05" */
export function mmss(seconds) {
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** 33 -> "33", 32.75 -> "32.75" */
export const fmtBar = (b) => (Number.isInteger(b) ? String(b) : String(+b.toFixed(2)));

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

const clone = (x) => JSON.parse(JSON.stringify(x));
const LEVEL_ORDER = { none: 0, info: 1, warning: 2, error: 3 };
const LEVEL_ICON = { error: "⛔", warning: "⚠", info: "ℹ" };

// Most decks a set can use (matches MAX_DECKS in models.py).
const MAX_DECKS = 6;
// Most timeline lanes (matches MAX_LANES in models.py). Lanes aren't decks: lanes take
// turns on a deck when one's track has finished (djplanner/lanes.py).
const MAX_LANES = 16;

/** Jog wheels in use (plans saved before this setting had one per deck). */
const jogCount = (settings) => settings.jog_wheels || settings.decks;



// ----- the Timeline -------------------------------------------------------------------

export class Timeline {
  /**
   * root:           element to draw into
   * onChange(plan): called after every edit (app.js: unsaved marker, autosave, checks)
   * getTrack(id):   async, returns full track detail (used to limit bar ranges)
   * onPreview(seg): play a block's audio (double-click, ▶ button or Space)
   */
  constructor({ root, onChange, getTrack, onPreview }) {
    this.root = root;
    this.onChange = onChange || (() => {});
    this.getTrack = getTrack;
    this.onPreview = onPreview || (() => {});
    this.playing = null; // {id, fraction} of the block being previewed, for its progress bar

    this.plan = emptyPlan();
    this.selectedId = null; // selected block (shows the editor)
    this.activeDeck = 1; // where clicked phrases get added
    this.pxPerBar = 6; // zoom level
    this.snapBars = 1; // grid snapping
    this.warnings = new Map(); // segment id (or "_plan") -> [{level, message}]
    this.history = []; // undo stack (snapshots)
    this.future = []; // redo stack
    this.drag = null; // {seg, isNew, grabBars} while something is being dragged
    this.solution = null; // latest solver answer {actions, cues, cost, issues}

    this._buildSkeleton();
    this.render();
  }

  // ===== public API used by app.js =================================================

  /** Replace the whole plan (e.g. after opening one). Clears undo history. */
  setPlan(plan, warnings = []) {
    this.plan = { ...emptyPlan(), ...clone(plan) };
    // Blocks from before deck lanes existed (e.g. an old unsaved draft) have no deck
    // or set position: lay them back to back on Deck 1, like the server does.
    let cursor = 1;
    for (const s of this.plan.segments) {
      if (s.set_bar == null) Object.assign(s, { deck: 1, set_bar: cursor });
      cursor = Math.max(cursor, setEnd(s));
    }
    this._sort();
    this.selectedId = null;
    this.history = [];
    this.future = [];
    this.setWarnings(warnings);
  }

  /** Show check results from the server: [{segment_id, level, message}]. */
  setWarnings(list) {
    this.warnings = new Map();
    for (const w of list || []) {
      const key = w.segment_id || "_plan";
      if (!this.warnings.has(key)) this.warnings.set(key, []);
      this.warnings.get(key).push({ level: w.level || "warning", message: w.message });
    }
    this.render();
  }

  /** Add a segment to the END of the active deck (or the end of the set if that
   *  deck is empty). Used when you click a phrase. */
  append(seg) {
    const onDeck = this.plan.segments.filter((s) => s.deck === this.activeDeck);
    const pool = onDeck.length ? onDeck : this.plan.segments;
    const at = pool.length ? Math.max(...pool.map(setEnd)) : 1;
    this.place(seg, this.activeDeck, at);
  }

  /** Put a NEW segment on a deck at a set bar, and select it. */
  place(seg, deck, setBar) {
    const s = { ...seg, deck, set_bar: Math.max(1, toBeat(setBar)) };
    this._edit(() => {
      this.plan.segments.push(s);
      this.selectedId = s.id;
      this.activeDeck = deck;
    });
    this._scrollToBlock(s.id);
  }

  /** Move an existing segment to a deck and set bar. */
  moveTo(id, deck, setBar) {
    const seg = this._get(id);
    if (!seg) return;
    const bar = Math.max(1, toBeat(setBar));
    if (seg.deck === deck && seg.set_bar === bar) return;
    this.activeDeck = deck;
    this._edit(() => Object.assign(seg, { deck, set_bar: bar }));
  }

  remove(id) {
    const i = this.plan.segments.findIndex((s) => s.id === id);
    if (i < 0) return;
    this._edit(() => {
      this.plan.segments.splice(i, 1);
      // select the neighbour so Delete can be pressed repeatedly
      const next = this.plan.segments[Math.min(i, this.plan.segments.length - 1)];
      this.selectedId = next ? next.id : null;
    });
  }

  /** Copy a block and put the copy straight after it on the same deck. */
  duplicate(id) {
    const seg = this._get(id);
    if (seg) this.place({ ...clone(seg), id: uid() }, seg.deck, setEnd(seg));
  }

  /** Change fields of a segment, e.g. update(id, {label: "Drop"}). */
  update(id, changes) {
    const seg = this._get(id);
    if (seg) this._edit(() => Object.assign(seg, changes));
  }

  setName(name) {
    this.plan.name = name; // not an undo step: it's typed in a text box
    this.onChange(this.plan);
  }

  /** Change set-wide settings (decks, jog wheels, instant doubles). */
  setSettings(changes) {
    this._edit(() => Object.assign(this.plan.settings, changes));
  }

  select(id) {
    this.selectedId = id;
    const seg = this._get(id);
    if (seg) this.activeDeck = seg.deck;
    this.render();
  }

  setActiveDeck(deck) {
    this.activeDeck = deck;
    this.render();
  }

  undo() {
    if (!this.history.length) return;
    this.future.push(this._snapshot());
    this._restore(this.history.pop());
  }

  redo() {
    if (!this.future.length) return;
    this.history.push(this._snapshot());
    this._restore(this.future.pop());
  }

  setZoom(pxPerBar) {
    this.pxPerBar = pxPerBar;
    this.render();
  }

  /** Keep the solver's latest answer (used for the step count in the header). */
  setSolution(solution) {
    this.solution = solution;
    this.render();
  }

  /** Show preview progress on a block (fraction 0-1), or clear it (id = null). */
  setPlaying(id, fraction = 0) {
    this.playing = id ? { id, fraction } : null;
    for (const b of this.root.querySelectorAll(".tl-block")) {
      const on = b.dataset.id === id;
      b.classList.toggle("playing", on);
      b.style.setProperty("--progress", on ? `${Math.min(1, Math.max(0, fraction)) * 100}%` : "0%");
    }
  }

  /** A phrase is being dragged in from a library strip (app.js calls this). */
  startExternalDrag(seg) {
    this.drag = { seg, isNew: true, grabBars: 0 };
  }

  /** Any drag finished (dropped or cancelled). */
  endDrag() {
    this.drag = null;
    this.ghost.hidden = true;
    this.root.querySelectorAll(".tl-block.dragging").forEach((b) => b.classList.remove("dragging"));
  }

  /** Keyboard shortcuts. app.js forwards keydown events here (when not typing).
   *  Returns true if the key was used. */
  handleKey(ev) {
    const ctrl = ev.ctrlKey || ev.metaKey;
    const key = ev.key.toLowerCase();
    if (ctrl && key === "z" && !ev.shiftKey) return this.undo(), true;
    if (ctrl && (key === "y" || (key === "z" && ev.shiftKey))) return this.redo(), true;
    const seg = this._get(this.selectedId);
    if (!seg) return false;
    if (ctrl && key === "d") return this.duplicate(seg.id), true;
    if (ev.key === "Delete" || ev.key === "Backspace") return this.remove(seg.id), true;
    if (ev.key === "Escape") return this.select(null), true;
    if (ev.altKey && (ev.key === "ArrowLeft" || ev.key === "ArrowRight")) {
      // nudge by one snap step
      const step = ev.key === "ArrowLeft" ? -this.snapBars : this.snapBars;
      return this.moveTo(seg.id, seg.deck, seg.set_bar + step), true;
    }
    if (ev.altKey && (ev.key === "ArrowUp" || ev.key === "ArrowDown")) {
      const deck = seg.deck + (ev.key === "ArrowUp" ? -1 : 1);
      if (deck >= 1 && deck <= this._laneCount()) this.moveTo(seg.id, deck, seg.set_bar);
      return true;
    }
    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      const segs = this.plan.segments;
      const i = segs.indexOf(seg) + (ev.key === "ArrowLeft" ? -1 : 1);
      if (segs[i]) this.select(segs[i].id);
      return true;
    }
    return false;
  }

  // ===== internals: state ==========================================================

  _get(id) {
    return this.plan.segments.find((s) => s.id === id);
  }

  /** Keep segments in playing order (set position, then deck). */
  _sort() {
    this.plan.segments.sort((a, b) => a.set_bar - b.set_bar || a.deck - b.deck);
  }

  /** Lanes to show: at least one per deck, and always one EMPTY lane below the last
   *  one in use (the user), so a new track can go in once another has finished: the
   *  solver puts it on that track's deck. */
  _laneCount() {
    const lastUsed = Math.max(0, ...this.plan.segments.map((s) => s.deck));
    return Math.min(MAX_LANES, Math.max(this.plan.settings.decks, lastUsed + 1));
  }

  _snapshot() {
    return JSON.stringify({ plan: this.plan, selectedId: this.selectedId });
  }

  _restore(snap) {
    const s = JSON.parse(snap);
    this.plan = s.plan;
    this.selectedId = s.selectedId;
    this.render();
    this.onChange(this.plan);
  }

  /** Run a change as one undoable step, then tidy up, redraw and notify. */
  _edit(fn) {
    this.history.push(this._snapshot());
    if (this.history.length > MAX_HISTORY) this.history.shift();
    this.future = [];
    fn();
    this._sort();
    this.render();
    this.onChange(this.plan);
  }

  // ===== internals: snapping =======================================================

  /**
   * Where should a block of `length` bars land if dragged to start at `raw`?
   * 1. If one of its edges is within SNAP_MAGNET_PX of another block's edge, stick
   *    to that edge (so lining up the end of one block with the start of another,
   *    or starting a mix exactly on a block boundary, is easy).
   * 2. Otherwise round to the snap grid.
   */
  _snap(raw, length, excludeId) {
    const threshold = SNAP_MAGNET_PX / this.pxPerBar;
    let best = null;
    let bestDist = threshold;
    for (const s of this.plan.segments) {
      if (s.id === excludeId) continue;
      for (const edge of [s.set_bar, setEnd(s)]) {
        for (const candidate of [edge, edge - length]) {
          const d = Math.abs(raw - candidate);
          if (d < bestDist) [best, bestDist] = [candidate, d];
        }
      }
    }
    const grid = 1 + Math.round((raw - 1) / this.snapBars) * this.snapBars;
    return Math.max(1, toBeat(best ?? grid));
  }

  /** Would a block at deck/setBar/length collide with another block on that deck? */
  _collides(deck, setBar, length, excludeId) {
    const probe = { set_bar: setBar, length_bars: length };
    return this.plan.segments.some((s) => s.id !== excludeId && s.deck === deck && overlap(s, probe) > 0);
  }

  // ===== internals: drawing ========================================================

  /** Build the parts that never change: header, scroll area, ghost, editor. */
  _buildSkeleton() {
    this.root.classList.add("timeline");

    // --- header: stats on the left, controls on the right ---
    const head = el("div", "tl-head");
    this.stats = el("span", "tl-stats muted");
    const controls = el("div", "tl-controls");

    this.undoBtn = el("button", "icon-btn", "↶");
    this.undoBtn.title = "Undo (Ctrl+Z)";
    this.undoBtn.addEventListener("click", () => this.undo());
    this.redoBtn = el("button", "icon-btn", "↷");
    this.redoBtn.title = "Redo (Ctrl+Y)";
    this.redoBtn.addEventListener("click", () => this.redo());

    const snap = el("label", "tl-ctl muted", "Snap ");
    const snapSel = el("select");
    for (const [bars, label] of SNAP_OPTIONS) {
      const o = el("option", null, label);
      o.value = bars;
      snapSel.append(o);
    }
    snapSel.value = this.snapBars;
    snapSel.addEventListener("change", () => (this.snapBars = +snapSel.value));
    snap.append(snapSel);

    const zoom = el("label", "tl-ctl muted", "Zoom ");
    const zoomInput = el("input");
    Object.assign(zoomInput, { type: "range", min: 2, max: 30, step: 1, value: this.pxPerBar });
    zoomInput.addEventListener("input", () => this.setZoom(+zoomInput.value));
    zoom.append(zoomInput);

    // Tracks = how many tracks can play at once, i.e. how many decks you have
    // (e.g. 3 for three CDJs). Stored as settings.decks.
    this.decksSelect = el("select");
    this.decksSelect.title = "How many tracks you can play at once (how many decks you have, e.g. 3 CDJs = 3)";
    for (let n = 1; n <= MAX_DECKS; n++) {
      const o = el("option", null, `${n} track${n > 1 ? "s" : ""}`);
      o.value = n;
      this.decksSelect.append(o);
    }
    this.decksSelect.addEventListener("change", () => {
      const decks = +this.decksSelect.value;
      const st = this.plan.settings;
      // If the jog wheels matched the tracks exactly (one each), keep it that way;
      // otherwise leave the jog wheel count as you set it.
      const jogs = jogCount(st) === st.decks ? decks : jogCount(st);
      this.setSettings({ decks, jog_wheels: jogs });
    });

    // Jog wheels = how many sets of deck controls you have (1-6). Fewer than tracks
    // means some decks share one and you switch between them (e.g. DDJ-FLX6-GT: 4
    // tracks on 2 jog wheels, 1/3 left and 2/4 right). As many or more = one each.
    this.jogsSelect = el("select");
    for (let n = 1; n <= MAX_DECKS; n++) {
      const o = el("option", null, `${n} jog wheel${n > 1 ? "s" : ""}`);
      o.value = n;
      this.jogsSelect.append(o);
    }
    this.jogsSelect.title =
      "How many jog wheels (sets of deck controls) you have. Fewer than tracks = decks share " +
      "one, switched with a deck-select button (e.g. DDJ-FLX6-GT: 4 tracks, 2 jog wheels)";
    this.jogsSelect.addEventListener("change", () => this.setSettings({ jog_wheels: +this.jogsSelect.value }));

    // Pre-roll = start each track this many bars early with its fader DOWN, so the moment
    // it comes in is just "fader up" (standard practice). Stored as settings.preroll_bars.
    this.prerollSelect = el("select");
    for (const [n, text] of [[0, "No pre-roll"], [1, "Pre-roll 1 bar"], [2, "Pre-roll 2 bars"],
      [4, "Pre-roll 4 bars"], [8, "Pre-roll 8 bars"]]) {
      const o = el("option", null, text);
      o.value = n;
      this.prerollSelect.append(o);
    }
    this.prerollSelect.title =
      "Start each track this many bars before it comes in, with its fader DOWN, so bringing it " +
      "in is just a fader move (it's already playing in time)";
    this.prerollSelect.addEventListener("change", () => this.setSettings({ preroll_bars: +this.prerollSelect.value }));

    const doubles = el("label", "check muted");
    this.doublesBox = el("input");
    this.doublesBox.type = "checkbox";
    this.doublesBox.addEventListener("change", () => this.setSettings({ instant_doubles: this.doublesBox.checked }));
    doubles.title = "Allow the same track on two decks at once";
    doubles.append(this.doublesBox, "Instant doubles");

    controls.append(this.undoBtn, this.redoBtn, snap, zoom, this.decksSelect, this.jogsSelect,
      this.prerollSelect, doubles);
    head.append(this.stats, controls);

    // --- scrolling area with ruler, deck lanes and mix row (filled by render) ---
    this.scroll = el("div", "tl-scroll");
    this.inner = el("div", "tl-inner");
    this.scroll.append(this.inner);
    this.ghost = el("div", "tl-ghost"); // preview of where a dragged block will land
    this.ghost.hidden = true;
    this._wireDragAndDrop();

    this.planWarnings = el("ul", "warnings tl-plan-warnings");
    this.editor = el("div", "tl-editor");
    this.root.append(head, this.scroll, this.planWarnings, this.editor);
  }

  render() {
    const segs = this.plan.segments;
    const px = this.pxPerBar;

    // --- header ---
    const endBar = segs.length ? Math.max(...segs.map(setEnd)) : 1;
    const bpm = segs[0]?.bpm || 0;
    const mixes = this._mixes();
    const tracks = new Set(segs.map((s) => s.track_id)).size;
    this.stats.textContent = segs.length
      ? `${fmtBar(endBar - 1)} bars` +
        (bpm ? ` · ~${mmss(((endBar - 1) * 4 * 60) / bpm)} at ${+bpm.toFixed(1)} BPM` : "") +
        ` · ${tracks} track${tracks > 1 ? "s" : ""} · ${mixes.length} mix${mixes.length === 1 ? "" : "es"}`
      : "Empty set";
    if (segs.length && this.solution) {
      const timed = this.solution.actions.filter((a) => a.kind !== "load").length;
      const newCues = this.solution.cues.filter((c) => !c.existing).length;
      this.stats.textContent += ` · ${timed} actions, ${newCues} new cue${newCues === 1 ? "" : "s"}`;
    }
    this.undoBtn.disabled = !this.history.length;
    this.redoBtn.disabled = !this.future.length;
    this.decksSelect.value = this.plan.settings.decks;
    this.jogsSelect.value = jogCount(this.plan.settings);
    this.doublesBox.checked = this.plan.settings.instant_doubles;
    this.prerollSelect.value = this.plan.settings.preroll_bars ?? 4;

    // --- size of the drawing area (in bars) ---
    const visibleBars = Math.ceil((this.scroll.clientWidth || 900) / px);
    const totalBars = Math.max(endBar - 1 + EXTRA_BARS, visibleBars);
    const widthPx = totalBars * px;

    const rows = [this._rulerRow(totalBars, widthPx)];
    for (let deck = 1; deck <= this._laneCount(); deck++) rows.push(this._laneRow(deck, widthPx));
    rows.push(this._mixRow(mixes, widthPx));
    this.inner.replaceChildren(...rows);

    // --- plan-level warnings (silence etc.) ---
    const general = this.warnings.get("_plan") || [];
    this.planWarnings.replaceChildren(
      ...general.map((w) => el("li", `lvl-${w.level}`, `${LEVEL_ICON[w.level] || ""} ${w.message}`))
    );

    this._renderEditor();
  }

  /** Bar numbers along the top. Picks a spacing that doesn't get crowded. */
  _rulerRow(totalBars, widthPx) {
    const row = el("div", "tl-row tl-ruler-row");
    row.append(el("div", "tl-lane-label muted", "Set bar"));
    const body = el("div", "tl-body tl-ruler");
    body.style.width = `${widthPx}px`;
    const step = [1, 2, 4, 8, 16, 32, 64].find((n) => n * this.pxPerBar >= 36) || 64;
    for (let bar = 1; bar <= totalBars; bar += step) {
      const tick = el("span", "tl-tick", String(bar));
      tick.style.left = `${(bar - 1) * this.pxPerBar}px`;
      body.append(tick);
    }
    row.append(body);
    return row;
  }

  /** One lane with its blocks. */
  _laneRow(deck, widthPx) {
    const row = el("div", "tl-row tl-lane");

    // Lanes are just lanes, not decks: the solver picks which physical deck each lane
    // is played on (solver.solve_best_decks), and the cheat sheet's "Tracks on each
    // deck" says where to load each track. So no deck numbers here (the user: a lane
    // reading "Deck 3" that ends up on Deck 4 would be confusing).
    const label = el("button", "tl-lane-label", `Lane ${deck}`);
    label.type = "button";
    if (deck === this.activeDeck) label.classList.add("active");
    label.title = "Make this the active lane (clicked phrases are added here).\n" +
      "Which deck it's played on is worked out for you: see the cheat sheet.";
    label.addEventListener("click", () => this.setActiveDeck(deck));

    const body = el("div", "tl-body");
    body.dataset.deck = deck;
    body.style.width = `${widthPx}px`;
    // background grid lines every 4 / 16 bars
    body.style.setProperty("--bar-px", `${this.pxPerBar}px`);

    for (const seg of this.plan.segments.filter((s) => s.deck === deck)) body.append(this._block(seg));
    if (!this.plan.segments.length && deck === 1) {
      body.append(el("div", "tl-empty muted",
        "Click a section on any track below to add it to the active lane, or drag it onto a lane."));
    }
    // clicking empty lane space deselects
    body.addEventListener("click", (ev) => {
      if (ev.target === body) {
        this.activeDeck = deck;
        this.select(null);
      }
    });
    row.append(label, body);
    return row;
  }

  /** One block. */
  _block(seg) {
    const px = this.pxPerBar;
    const b = el("div", "tl-block");
    b.dataset.id = seg.id;
    b.draggable = true;
    b.tabIndex = 0;
    b.style.left = `${(seg.set_bar - 1) * px}px`;
    b.style.width = `${Math.max(seg.length_bars * px, MIN_BLOCK_PX)}px`;
    b.style.setProperty("--block-color", phraseColor(seg.kind));
    if (seg.id === this.selectedId) b.classList.add("selected");
    if (this.playing?.id === seg.id) {
      b.classList.add("playing");
      b.style.setProperty("--progress", `${this.playing.fraction * 100}%`);
    }

    // Worst problem on this block decides its border style
    const warns = this.warnings.get(seg.id) || [];
    const worst = warns.reduce((w, x) => (LEVEL_ORDER[x.level] > LEVEL_ORDER[w] ? x.level : w), "none");
    if (worst !== "none") b.classList.add(`has-${worst}`);

    const trackRange = `${fmtBar(seg.start_bar)}–${fmtBar(seg.start_bar + seg.length_bars - 1)}`;
    b.title =
      `${seg.label} · ${seg.track_title}\n` +
      `Lane ${seg.deck} · set bars ${fmtBar(seg.set_bar)}–${fmtBar(setEnd(seg) - 1)}\n` +
      `Track bars ${trackRange} (${fmtBar(seg.length_bars)} bars)\nDouble-click to listen` +
      warns.map((w) => `\n${LEVEL_ICON[w.level]} ${w.message}`).join("");

    b.append(
      el("span", "tl-label", `${worst !== "none" ? LEVEL_ICON[worst] + " " : ""}${seg.label}`),
      el("span", "tl-track", seg.track_title),
      el("span", "tl-len", `${fmtBar(seg.length_bars)} bars · trk ${trackRange}`)
    );

    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this.select(seg.id);
    });
    b.addEventListener("dblclick", (ev) => {
      ev.stopPropagation();
      this.onPreview(seg);
    });
    b.addEventListener("dragstart", (ev) => {
      // remember where in the block it was grabbed, so it doesn't jump under the cursor
      const grabBars = (ev.clientX - b.getBoundingClientRect().left) / this.pxPerBar;
      this.drag = { seg, isNew: false, grabBars };
      ev.dataTransfer.setData(DRAG_MOVE, seg.id);
      ev.dataTransfer.effectAllowed = "move";
      requestAnimationFrame(() => b.classList.add("dragging"));
    });
    b.addEventListener("dragend", () => this.endDrag());
    return b;
  }

  /** Every place where two decks play at once. */
  _mixes() {
    const segs = this.plan.segments;
    const out = [];
    for (let i = 0; i < segs.length; i++) {
      for (let j = i + 1; j < segs.length; j++) {
        const [a, b] = [segs[i], segs[j]];
        const bars = overlap(a, b);
        if (a.deck !== b.deck && bars > 0) {
          out.push({ start: Math.max(a.set_bar, b.set_bar), bars, a, b });
        }
      }
    }
    return out;
  }

  /** Row under the lanes showing each mix as a band ("8-bar mix"). */
  _mixRow(mixes, widthPx) {
    const row = el("div", "tl-row tl-mix-row");
    row.append(el("div", "tl-lane-label muted", "Mixes"));
    const body = el("div", "tl-body");
    body.style.width = `${widthPx}px`;
    for (const m of mixes) {
      const band = el("div", "tl-mix", `${fmtBar(m.bars)}-bar mix`);
      band.style.left = `${(m.start - 1) * this.pxPerBar}px`;
      band.style.width = `${m.bars * this.pxPerBar}px`;
      band.title = `Lane ${m.a.deck} '${m.a.label}' + Lane ${m.b.deck} '${m.b.label}'\n` +
        `set bars ${fmtBar(m.start)}–${fmtBar(m.start + m.bars - 1)}`;
      body.append(band);
    }
    row.append(body);
    return row;
  }

  // ===== internals: drag and drop ==================================================

  /** Work out deck + set bar for a drag at clientX over a lane body. */
  _dropTarget(ev) {
    const body = ev.target.closest?.(".tl-body[data-deck]");
    if (!body || !this.drag) return null;
    const deck = +body.dataset.deck;
    const rect = body.getBoundingClientRect();
    const pointerBar = 1 + (ev.clientX - rect.left) / this.pxPerBar;
    const { seg, isNew, grabBars } = this.drag;
    const excludeId = isNew ? null : seg.id;
    const setBar = this._snap(pointerBar - grabBars, seg.length_bars, excludeId);
    return { body, deck, setBar, collides: this._collides(deck, setBar, seg.length_bars, excludeId) };
  }

  _wireDragAndDrop() {
    this.scroll.addEventListener("dragover", (ev) => {
      const t = this._dropTarget(ev);
      if (!t) return (this.ghost.hidden = true);
      ev.preventDefault(); // allows the drop
      ev.dataTransfer.dropEffect = this.drag.isNew ? "copy" : "move";

      // show the ghost where the block would land (red if it hits another block)
      if (this.ghost.parentElement !== t.body) t.body.append(this.ghost);
      this.ghost.style.left = `${(t.setBar - 1) * this.pxPerBar}px`;
      this.ghost.style.width = `${Math.max(this.drag.seg.length_bars * this.pxPerBar, MIN_BLOCK_PX)}px`;
      this.ghost.classList.toggle("bad", t.collides);
      this.ghost.textContent = `bar ${fmtBar(t.setBar)}`;
      this.ghost.hidden = false;

      // auto-scroll near the edges
      const r = this.scroll.getBoundingClientRect();
      if (ev.clientX < r.left + 90) this.scroll.scrollLeft -= 14;
      else if (ev.clientX > r.right - 40) this.scroll.scrollLeft += 14;
    });

    this.scroll.addEventListener("dragleave", (ev) => {
      if (!this.scroll.contains(ev.relatedTarget)) this.ghost.hidden = true;
    });

    this.scroll.addEventListener("drop", (ev) => {
      const t = this._dropTarget(ev);
      if (!t) return;
      ev.preventDefault();
      const { seg, isNew } = this.drag;
      this.endDrag();
      if (isNew) this.place({ ...seg, id: uid() }, t.deck, t.setBar);
      else this.moveTo(seg.id, t.deck, t.setBar);
    });
  }

  _scrollToBlock(id) {
    this.root.querySelector(`.tl-block[data-id="${id}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  // ===== internals: editor =========================================================

  /** Panel under the lanes for editing the selected block. */
  async _renderEditor() {
    const seg = this._get(this.selectedId);
    this.editor.replaceChildren();
    this.editor.hidden = !seg;
    if (!seg) return;

    const title = el("div", "tl-ed-title");
    const sw = el("span", "swatch");
    sw.style.background = phraseColor(seg.kind);
    title.append(sw, el("strong", null, seg.label), ` · ${seg.track_title}`);
    if (seg.track_artist) title.append(el("span", "muted", ` · ${seg.track_artist}`));

    const form = el("div", "tl-ed-fields");
    const field = (labelText, input, hint) => {
      const l = el("label", null, labelText);
      l.append(input);
      if (hint) l.title = hint;
      form.append(l);
      return input;
    };
    const num = (value, step, min) => {
      const i = el("input");
      Object.assign(i, { type: "number", step, value });
      if (min != null) i.min = min;
      return i;
    };

    const nameIn = field("Name", el("input"));
    nameIn.value = seg.label;
    nameIn.addEventListener("change", () => this.update(seg.id, { label: nameIn.value.trim() || seg.label }));

    const deckSel = field("Lane", el("select"));
    for (let d = 1; d <= this._laneCount(); d++) {
      const o = el("option", null, String(d));
      o.value = d;
      deckSel.append(o);
    }
    deckSel.value = seg.deck;
    deckSel.addEventListener("change", () => this.moveTo(seg.id, +deckSel.value, seg.set_bar));

    const setIn = field("Set bar", num(seg.set_bar, 0.25, 1), "Where in the set this block starts");
    setIn.addEventListener("change", () => this.moveTo(seg.id, seg.deck, +setIn.value));

    const startIn = field("Track bar", num(seg.start_bar, 0.25), "Which bar of the track it starts from");
    const lenIn = field("Length", num(seg.length_bars, 0.25, 0.25), "Length in bars");
    const note = el("span", "muted", `plays track bars ${fmtBar(seg.start_bar)}–${fmtBar(seg.start_bar + seg.length_bars - 1)}`);
    form.append(note);

    // Changing the track range turns a phrase block into a custom one.
    const applyRange = () => {
      const start = toBeat(+startIn.value);
      const length = Math.max(0.25, toBeat(+lenIn.value));
      if (start !== seg.start_bar || length !== seg.length_bars) {
        this.update(seg.id, { start_bar: start, length_bars: length, source: "custom" });
      }
    };
    startIn.addEventListener("change", applyRange);
    lenIn.addEventListener("change", applyRange);

    const actions = el("div", "tl-ed-actions");
    const btn = (text, tip, fn) => {
      const b = el("button", null, text);
      b.type = "button";
      b.title = tip;
      b.addEventListener("click", fn);
      actions.append(b);
    };
    btn("▶ Preview", "Listen to this block (Space, or double-click the block)", () => this.onPreview(seg));
    btn("Duplicate", "Put a copy straight after it on the same lane (Ctrl+D)", () => this.duplicate(seg.id));
    btn("Remove", "Remove from the set (Delete)", () => this.remove(seg.id));

    const warnList = el("ul", "warnings");
    for (const w of this.warnings.get(seg.id) || []) {
      warnList.append(el("li", `lvl-${w.level}`, `${LEVEL_ICON[w.level]} ${w.message}`));
    }
    this.editor.append(title, form, actions, warnList);

    // Limit the track-bar inputs to the track's length (needs the track detail).
    if (this.getTrack) {
      try {
        const t = await this.getTrack(seg.track_id);
        if (this.selectedId !== seg.id) return; // selection changed while loading
        startIn.min = Math.min(t.first_bar, 1);
        startIn.max = t.bar_count;
        lenIn.max = t.bar_count + 1 - seg.start_bar;
        note.textContent += ` (track has ${t.bar_count} bars)`;
      } catch {
        note.textContent += " (track not in library)";
      }
    }
  }
}
