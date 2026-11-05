// Phrase editor: edit a track's Custom sections right on its phrase strip.
//
//   drag an edge          -> move it (snaps to whole bars). Between two touching sections
//                            only ONE moves: drag left = the left one shrinks, drag right
//                            = the right one shrinks (a gap opens). Shift+drag moves both.
//   click a section       -> select it (then change its type, split it in half, add a
//                            1-bar section after it, merge or delete it: buttons below)
//   drag a section's middle -> slide it sideways into an empty gap next to it
//   drag an edge across a section -> deletes it
//   double-click          -> split the section at that bar
//   drag across a gap     -> add a new section there
//   right-click           -> listen, as usual (handled by app.js)
//   Delete / Ctrl+Z / Esc -> delete the selected section / undo / deselect
//
// Nothing is saved until you press Save. Saved sections go to custom_phrases.json via
// the API (never to Rekordbox); see djplanner/custom_phrases.py.
//
// The first half of this file is plain EDITING LOGIC (pure functions on a list of
// sections, no page or canvas involved) so it can be tested on its own:
// tests/js/test_phrase_editor.mjs. The PhraseEditor class below wires it to the page.
//
// In here a section is {kind, start, end}: `end` is the bar just AFTER the section
// (so bars 17-32 = {start: 17, end: 33}). The saved format is [name, first, last] with
// the last bar included, like phrase_labels.json.

/** Section types you can pick, in menu order, with their display names. */
export const KINDS = [
  ["intro", "Intro"],
  ["build", "Build"],
  ["predrop", "Pre-drop"],
  ["fakedrop", "Fake drop"],
  ["drop", "Drop"],
  ["groove", "Groove"],
  ["breakdown", "Breakdown"],
  ["outro", "Outro"],
];
const NAME_OF = Object.fromEntries(KINDS);
const KIND_OF = Object.fromEntries(KINDS.flatMap(([k, n]) => [[k, k], [n.toLowerCase(), k]]));

// ==================================================================================
// Editing logic (pure: every function returns a NEW list and never changes its input)
// ==================================================================================

const copy = (secs) => secs.map((s) => ({ ...s }));
const byStart = (a, b) => a.start - b.start;

/** Saved [[name, first, last], ...] -> editor sections, sorted. */
export function fromSaved(saved) {
  return (saved || [])
    .map(([name, first, last]) => ({ kind: KIND_OF[String(name).toLowerCase()] || "intro", start: first, end: last + 1 }))
    .sort(byStart);
}

/** Editor sections -> saved [[name, first, last], ...]. */
export function toSaved(secs) {
  return [...secs].sort(byStart).map((s) => [NAME_OF[s.kind], s.start, s.end - 1]);
}

/**
 * The section edge nearest to bar position `pos`, if it's within `tol` bars.
 * Returns {bar, left, right}: `left` = index of the section that ENDS there, `right` =
 * the one that STARTS there (-1 if none, e.g. next to a gap). Null if none is close.
 * An edge's grab zone reaches at most a third of the way into a section, so even a
 * 1-bar section keeps a middle you can grab to slide it.
 */
export function findEdge(secs, pos, tol) {
  let best = null;
  for (const s of secs) {
    const reach = Math.min(tol, (s.end - s.start) / 3);
    for (const bar of [s.start, s.end]) {
      const d = Math.abs(pos - bar);
      // inside this section: only near its own edge (reach); outside it: the full tol
      const inside = pos >= s.start && pos < s.end;
      if (d <= (inside ? reach : tol) && (!best || d < best.d)) best = { bar, d };
    }
  }
  if (!best) return null;
  // an edge between two sections: the pointer is inside one of them, so that one's
  // reach applies (checked above via whichever section the pointer is in)
  const owner = secs.find((s) => pos >= s.start && pos < s.end);
  if (owner && best.d > Math.min(tol, (owner.end - owner.start) / 3)) return null;
  return {
    bar: best.bar,
    left: secs.findIndex((s) => s.end === best.bar),
    right: secs.findIndex((s) => s.start === best.bar),
  };
}

/**
 * Move an edge (from findEdge) to `toBar`, rounded to a whole bar. Sections can't
 * overlap and stay between bar 1 and `maxEnd` (the bar just after the track's last bar).
 * Where two sections touch, only ONE of them moves (the user's choice, 2026-10-15):
 * dragging left shrinks the left section, dragging right shrinks the right one, and a
 * gap opens between them. An edge next to a gap can also grow into the gap.
 * Drag an edge right across its section (to or past its other edge) and the section is
 * DELETED (the user: "just delete them", rather than stopping at 1 bar).
 * together = true (Shift+drag) moves a shared edge for both sections instead.
 */
export function moveEdge(secs, edge, toBar, maxEnd, together = false) {
  const out = copy(secs);
  let { left, right } = edge;
  const bar = Math.round(toBar);
  if (together && left >= 0 && right >= 0) {
    const b = Math.min(out[right].end - 1, Math.max(out[left].start + 1, bar));
    out[left].end = b;
    out[right].start = b;
    return out;
  }
  if (left >= 0 && right >= 0) {
    if (bar < edge.bar) right = -1; // dragging left: only the left section's end moves
    else if (bar > edge.bar) left = -1; // dragging right: only the right section's start moves
    else return out;
  }
  if (left >= 0) {
    // moving the END of section `left`: grows up to the next section, deleted if run over
    if (bar <= out[left].start) return out.filter((_, j) => j !== left);
    const hi = Math.min(maxEnd, ...out.filter((s) => s.start >= edge.bar).map((s) => s.start));
    out[left].end = Math.min(hi, bar);
  } else if (right >= 0) {
    // moving the START of section `right`: grows back to the section before, deleted if run over
    if (bar >= out[right].end) return out.filter((_, j) => j !== right);
    const lo = Math.max(1, ...out.filter((s) => s.end <= edge.bar).map((s) => s.end));
    out[right].start = Math.max(lo, bar);
  }
  return out;
}

/**
 * Slide section i sideways by `delta` bars (rounded), keeping its length, into the
 * empty space next to it: it stops at its neighbours, bar 1 and the end of the track.
 */
export function moveSection(secs, i, delta, maxEnd) {
  const s = secs[i];
  if (!s) return copy(secs);
  const len = s.end - s.start;
  const others = secs.filter((_, j) => j !== i);
  const lo = Math.max(1, ...others.filter((x) => x.end <= s.start).map((x) => x.end));
  const hi = Math.min(maxEnd, ...others.filter((x) => x.start >= s.end).map((x) => x.start));
  const start = Math.min(hi - len, Math.max(lo, s.start + Math.round(delta)));
  const out = copy(secs);
  out[i].start = start;
  out[i].end = start + len;
  return out;
}

/** Index of the section containing bar position `pos`, or -1. */
export function sectionAt(secs, pos) {
  return secs.findIndex((s) => pos >= s.start && pos < s.end);
}

/**
 * Split the section containing `bar` so the second part starts at `bar` (a whole
 * bar). Returns {secs, index} (index = the new second part), or null if `bar` isn't
 * strictly inside a section.
 */
export function splitAt(secs, bar) {
  const i = secs.findIndex((s) => bar > s.start && bar < s.end);
  if (i < 0) return null;
  const out = copy(secs);
  const s = out[i];
  out.splice(i, 1, { ...s, end: bar }, { ...s, start: bar });
  return { secs: out, index: i + 1 };
}

/** The bar in the middle of section i (where "Split" cuts by default), or null if it's 1 bar. */
export function midBar(secs, i) {
  const s = secs[i];
  if (!s || s.end - s.start < 2) return null;
  return s.start + Math.ceil((s.end - s.start) / 2); // 8 bars -> 4 + 4; 7 bars -> 4 + 3
}

/** A sensible type for a section added straight after one of type `kind`. */
export function nextKindAfter(kind) {
  return kind === "build" ? "predrop" : kind === "predrop" ? "drop" : "build";
}

/**
 * Add a 1-bar section straight after section i (for short builds / pre-drops). If
 * there's a gap after it, the new section takes the gap's first bar; otherwise it takes
 * section i's LAST bar (section i gets 1 bar shorter). Returns {secs, index} (index =
 * the new section) or null if there's no room (section i is 1 bar and touches the next,
 * or it ends at the end of the track... and is 1 bar).
 */
export function addAfter(secs, i, kind, maxEnd) {
  const s = secs[i];
  if (!s) return null;
  const out = copy(secs);
  const next = out.filter((x) => x.start >= s.end).sort(byStart)[0];
  const gapEnd = next ? next.start : maxEnd;
  let start;
  if (gapEnd > s.end) {
    start = s.end; // room in the gap after it
  } else if (s.end - s.start >= 2) {
    start = s.end - 1; // take its last bar
    out[i].end = start;
  } else {
    return null;
  }
  out.push({ kind, start, end: start + 1 });
  out.sort(byStart);
  return { secs: out, index: out.findIndex((x) => x.start === start) };
}

/**
 * Merge section i with its neighbour (dir = -1 before, +1 after), if they touch.
 * The merged section keeps section i's type. Returns {secs, index} or null.
 */
export function mergeWith(secs, i, dir) {
  const j = i + dir;
  if (i < 0 || j < 0 || j >= secs.length) return null;
  const [a, b] = dir > 0 ? [secs[i], secs[j]] : [secs[j], secs[i]];
  if (a.end !== b.start) return null; // a gap between them: nothing to merge
  const out = copy(secs);
  const lo = Math.min(i, j);
  out.splice(lo, 2, { kind: secs[i].kind, start: a.start, end: b.end });
  return { secs: out, index: lo };
}

/** Change section i's type. */
export function setKind(secs, i, kind) {
  const out = copy(secs);
  if (out[i] && NAME_OF[kind]) out[i].kind = kind;
  return out;
}

/** Remove section i (leaves a gap). */
export function removeAt(secs, i) {
  const out = copy(secs);
  out.splice(i, 1);
  return out;
}

/**
 * Add a section covering bars `fromBar`..`toBar` (both included, either order), cut
 * down to fit the gap that `fromBar` is in. Returns {secs, index} or null if `fromBar`
 * is inside an existing section or nothing fits.
 */
export function addSection(secs, fromBar, toBar, kind, maxEnd) {
  const a = Math.floor(Math.min(fromBar, toBar));
  const b = Math.floor(Math.max(fromBar, toBar)) + 1; // bar just after
  const anchor = Math.floor(fromBar);
  if (sectionAt(secs, anchor) >= 0) return null;
  const lo = Math.max(1, ...secs.filter((s) => s.end <= anchor).map((s) => s.end));
  const hi = Math.min(maxEnd, ...secs.filter((s) => s.start > anchor).map((s) => s.start));
  const start = Math.max(a, lo);
  const end = Math.min(b, hi);
  if (end <= start) return null;
  const out = [...copy(secs), { kind, start, end }].sort(byStart);
  return { secs: out, index: out.findIndex((s) => s.start === start) };
}

/**
 * Labels the way the Auto (EDM) detection names them: "Drop 1", "Drop 2"... where a
 * type appears in several separate places. Intro/Outro are never numbered, and
 * back-to-back sections of the same type share a label.
 */
export function numberedLabels(secs) {
  const runs = [];
  secs.forEach((s, i) => {
    const prev = runs.at(-1);
    if (prev && secs[prev[0]].kind === s.kind && secs[prev.at(-1)].end === s.start) prev.push(i);
    else runs.push([i]);
  });
  const totals = {};
  for (const r of runs) totals[secs[r[0]].kind] = (totals[secs[r[0]].kind] || 0) + 1;
  const seen = {};
  const labels = [];
  for (const r of runs) {
    const k = secs[r[0]].kind;
    seen[k] = (seen[k] || 0) + 1;
    const base = totals[k] === 1 || k === "intro" || k === "outro" ? NAME_OF[k] : `${NAME_OF[k]} ${seen[k]}`;
    for (const i of r) labels[i] = base;
  }
  return labels;
}

/** Editor sections -> phrase objects shaped like the API's, for drawing/hovering/playing. */
export function toPhrases(secs) {
  const sorted = [...secs].sort(byStart);
  const labels = numberedLabels(sorted);
  return sorted.map((s, i) => ({
    kind: s.kind,
    label: labels[i],
    start_bar: s.start,
    length_bars: s.end - s.start,
    end_bar: s.end,
    fill_start_bar: null,
    source: "custom",
  }));
}

// ==================================================================================
// The editor on the page
// ==================================================================================

const EDGE_GRAB_PX = 6; // how close (pixels) the pointer must be to grab an edge
const UNDO_LIMIT = 100;

export class PhraseEditor {
  /**
   * li:      the track's card;  canvas: its phrase strip
   * track:   the track detail (bar_count, bar_times, ...)
   * entry:   {sections, note, based_on} to start from (from the API)
   * barAt(clientX) -> bar position under the pointer (app.js: hitTest)
   * onChange()          redraw the strip (it asks this editor for view()/highlight())
   * onSave(body)        save {sections, note, based_on}; returns a Promise
   * getDetected()       Promise of the Auto (EDM) sections, for "Reset to Auto (EDM)"
   * onClose(saved)      the editor closed (saved = true after a successful Save)
   */
  constructor({ li, canvas, track, entry, barAt, onChange, onSave, getDetected, onClose }) {
    Object.assign(this, { li, canvas, track, barAt, onChange, onSave, getDetected, onClose });
    this.trackId = track.id;
    this.maxEnd = (track.bar_count || 1) + 1;
    this.secs = fromSaved(entry.sections);
    this.basedOn = entry.based_on || "blank";
    this.savedJson = JSON.stringify({ s: toSaved(this.secs), n: entry.note || "" });
    this.selected = -1; // index into this.secs, -1 = nothing selected
    this.undoStack = [];
    this.redoStack = [];
    this.drag = null; // {type: "edge" | "add", ...} while the left button is held
    this.pending = null; // the section being drawn by an "add" drag
    this.lastKind = "intro"; // type given to new sections
    this.focused = true; // keyboard shortcuts go to the editor (until you click elsewhere)

    this._buildBar(entry.note || "");
    this._listen();
    this._refresh();
  }

  // ----- what app.js draws -------------------------------------------------------

  /** The track as it looks right now (with the edited sections), for drawing/hover/play. */
  view() {
    const secs = this.pending ? [...this.secs, this.pending] : this.secs;
    return { ...this.track, phrases: toPhrases(secs) };
  }

  /** The selected section, as a phrase from view(), to outline. */
  highlight() {
    const s = this.secs[this.selected];
    return s ? this.view().phrases.find((p) => p.start_bar === s.start) || null : null;
  }

  get dirty() {
    return JSON.stringify({ s: toSaved(this.secs), n: this.noteInput.value }) !== this.savedJson;
  }

  // ----- changes (all go through here so Undo works) -----------------------------

  _apply(secs, selected = this.selected) {
    this.undoStack.push({ secs: this.secs, selected: this.selected });
    if (this.undoStack.length > UNDO_LIMIT) this.undoStack.shift();
    this.redoStack = [];
    this.secs = secs;
    this.selected = selected;
    this.statusText.classList.remove("error"); // a new change clears an old error
    this._refresh();
  }

  undo() {
    const prev = this.undoStack.pop();
    if (!prev) return;
    this.redoStack.push({ secs: this.secs, selected: this.selected });
    ({ secs: this.secs, selected: this.selected } = prev);
    this._refresh();
  }

  redo() {
    const next = this.redoStack.pop();
    if (!next) return;
    this.undoStack.push({ secs: this.secs, selected: this.selected });
    ({ secs: this.secs, selected: this.selected } = next);
    this._refresh();
  }

  select(i) {
    this.selected = i >= 0 && i < this.secs.length ? i : -1;
    this._refresh();
  }

  setSelectedKind(kind) {
    if (this.selected < 0) return;
    this.lastKind = kind;
    this._apply(setKind(this.secs, this.selected, kind));
  }

  mergeSelected(dir) {
    const r = mergeWith(this.secs, this.selected, dir);
    if (r) this._apply(r.secs, r.index);
  }

  deleteSelected() {
    if (this.selected >= 0) this._apply(removeAt(this.secs, this.selected), -1);
  }

  splitAtBar(bar) {
    const r = splitAt(this.secs, Math.round(bar));
    if (r) this._apply(r.secs, r.index);
  }

  /** The "Split in half" button: splits the selected section 50/50 (then move the parts). */
  splitSelected() {
    const bar = midBar(this.secs, this.selected);
    if (bar === null) return;
    const r = splitAt(this.secs, bar);
    if (r) this._apply(r.secs, r.index - 1); // keep the first half selected
  }

  /** The "+ Add after" button: a 1-bar section straight after the selected one. */
  addAfterSelected() {
    const s = this.secs[this.selected];
    if (!s) return;
    const r = addAfter(this.secs, this.selected, nextKindAfter(s.kind), this.maxEnd);
    if (r) this._apply(r.secs, r.index);
    else this._status("No room: the section is 1 bar long and something comes straight after it", true);
  }

  // ----- the toolbar under the strip ---------------------------------------------

  _buildBar(note) {
    const mk = (tag, cls, text) => {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    };
    const button = (text, title, fn, cls) => {
      const b = mk("button", cls, text);
      b.type = "button";
      b.title = title;
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        fn();
      });
      return b;
    };

    const bar = mk("div", "phrase-editor");
    // Row 1: what's selected, and what you can do to it
    const row1 = mk("div", "pe-row");
    row1.append(mk("strong", "pe-title", "✎ Editing your Custom sections"));
    this.selText = mk("span", "pe-sel muted");
    this.kindSelect = mk("select");
    this.kindSelect.title = "Section type";
    for (const [k, n] of KINDS) {
      const o = mk("option", null, n);
      o.value = k;
      this.kindSelect.append(o);
    }
    this.kindSelect.addEventListener("change", () => this.setSelectedKind(this.kindSelect.value));
    this.mergePrevBtn = button("◀ Merge", "Merge with the section before (keeps this one's type)", () => this.mergeSelected(-1));
    this.mergeNextBtn = button("Merge ▶", "Merge with the section after (keeps this one's type)", () => this.mergeSelected(1));
    this.deleteBtn = button("Delete", "Remove this section (Delete key)", () => this.deleteSelected());
    this.splitBtn = button("Split in half", "Split the selected section 50/50 (then drag the edges/parts)",
      () => this.splitSelected());
    this.addAfterBtn = button("+ Add after", "Add a 1-bar section straight after this one " +
      "(takes its last bar if nothing's free); then pick its type", () => this.addAfterSelected());
    this.undoBtn = button("↶ Undo", "Undo (Ctrl+Z)", () => this.undo());
    this.redoBtn = button("↷ Redo", "Redo (Ctrl+Y)", () => this.redo());
    row1.append(this.selText, this.kindSelect, this.splitBtn, this.addAfterBtn,
      this.mergePrevBtn, this.mergeNextBtn, this.deleteBtn, mk("span", "pe-gap"), this.undoBtn, this.redoBtn);

    // Row 2: note, start again, and save / cancel
    const row2 = mk("div", "pe-row");
    this.noteInput = mk("input", "pe-note");
    this.noteInput.type = "text";
    this.noteInput.maxLength = 2000;
    this.noteInput.placeholder = "Note (optional): why, e.g. “bass still in at 41, so it's drop”";
    this.noteInput.value = note;
    this.noteInput.addEventListener("input", () => this._refresh());
    this.statusText = mk("span", "pe-status muted");
    this.saveBtn = button("Save", "Save your sections for this track", () => this.save(), "primary");
    row2.append(
      this.noteInput,
      button("Reset to Auto (EDM)", "Start again from the Auto (EDM) sections (only kept if you Save)", () => this.resetToDetected()),
      button("Clear all", "Remove every section (only kept if you Save)", () => {
        if (this.secs.length) this._apply([], -1);
      }),
      this.statusText,
      button("Cancel", "Close without saving", () => this.cancel()),
      this.saveBtn
    );

    const hint = mk("div", "pe-hint muted",
      "Drag an edge to move it (snaps to bars; Shift+drag moves both touching sections; drag it right " +
      "across a section to delete it) · drag a section's middle (or Alt+drag) to slide it into a gap · " +
      "click a section to select it · double-click to split it " +
      "at that bar · drag across an empty part to add a section (start away from other sections' edges) · " +
      "right-click to listen · " +
      "Delete removes · Ctrl+Z undoes");
    bar.append(row1, row2, hint);
    bar.addEventListener("pointerdown", () => (this.focused = true));
    this.canvas.after(bar);
    this.bar = bar;
  }

  /** Bring the toolbar up to date and redraw the strip. */
  _refresh() {
    const s = this.secs[this.selected];
    const has = !!s;
    this.kindSelect.disabled = this.deleteBtn.disabled = !has;
    if (has) {
      this.kindSelect.value = s.kind;
      this.selText.textContent = `Bars ${s.start}–${s.end - 1} (${s.end - s.start} bars):`;
    } else {
      this.selText.textContent = this.secs.length
        ? "Click a section to select it."
        : "No sections yet: drag across the strip to add one.";
    }
    this.splitBtn.disabled = !has || midBar(this.secs, this.selected) === null;
    this.addAfterBtn.disabled = !(has && addAfter(this.secs, this.selected, "build", this.maxEnd));
    this.mergePrevBtn.disabled = !(has && mergeWith(this.secs, this.selected, -1));
    this.mergeNextBtn.disabled = !(has && mergeWith(this.secs, this.selected, 1));
    this.undoBtn.disabled = !this.undoStack.length;
    this.redoBtn.disabled = !this.redoStack.length;
    if (!this.statusText.classList.contains("error")) {
      this.statusText.textContent = this.dirty ? "Unsaved changes" : "";
    }
    this.onChange();
  }

  _status(text, isError = false) {
    this.statusText.textContent = text;
    this.statusText.classList.toggle("error", isError);
  }

  async resetToDetected() {
    try {
      const secs = fromSaved(await this.getDetected());
      this.basedOn = "detected";
      this._apply(secs, -1);
    } catch (e) {
      this._status(`Couldn't get the Auto (EDM) sections: ${e.message}`, true);
    }
  }

  async save() {
    this.saveBtn.disabled = true;
    this._status("Saving…");
    try {
      await this.onSave({ sections: toSaved(this.secs), note: this.noteInput.value, based_on: this.basedOn });
    } catch (e) {
      this.saveBtn.disabled = false;
      return this._status(`Not saved: ${e.message}`, true);
    }
    this.savedJson = JSON.stringify({ s: toSaved(this.secs), n: this.noteInput.value });
    this.close(true);
  }

  cancel() {
    if (this.dirty && !confirm("Close the editor without saving your changes?")) return;
    this.close(false);
  }

  /** Remove the toolbar and listeners. app.js redraws the strip with the track's saved sections. */
  close(saved = false) {
    this._unlisten();
    this.bar.remove();
    this.closed = true;
    this.onClose(saved);
  }

  // ----- pointer + keyboard ----------------------------------------------------------

  /** How many bars EDGE_GRAB_PX pixels are on this strip right now. */
  _tolBars() {
    const left = this.canvas.getBoundingClientRect().left;
    return Math.abs(this.barAt(left + EDGE_GRAB_PX) - this.barAt(left));
  }

  _listen() {
    const c = this.canvas;
    this._handlers = {
      pointerdown: (ev) => {
        if (ev.button !== 0) return; // right button = listen (app.js)
        this.focused = true;
        const pos = this.barAt(ev.clientX);
        // (Alt+drag always slides the section, even when you're on its edge)
        const edge = ev.altKey ? null : findEdge(this.secs, pos, this._tolBars());
        if (edge) {
          c.setPointerCapture(ev.pointerId);
          this.drag = { type: "edge", edge, base: this.secs, moved: false, together: ev.shiftKey };
          return;
        }
        const i = sectionAt(this.secs, pos);
        if (i >= 0) {
          // select it, and if you drag, slide it sideways (only into empty space)
          c.setPointerCapture(ev.pointerId);
          this.drag = { type: "move", i, from: pos, base: this.secs, moved: false };
          return this.select(i);
        }
        // empty space: start drawing a new section
        c.setPointerCapture(ev.pointerId);
        this.drag = { type: "add", from: pos };
        this.pending = null;
        this._updateAdd(pos);
      },
      pointermove: (ev) => {
        const pos = this.barAt(ev.clientX);
        if (!this.drag) {
          // show a resize cursor over edges
          c.style.cursor = findEdge(this.secs, pos, this._tolBars()) ? "col-resize"
            : sectionAt(this.secs, pos) >= 0 ? "grab" : "";
          return;
        }
        if (this.drag.type === "move") {
          const moved = moveSection(this.drag.base, this.drag.i, pos - this.drag.from, this.maxEnd);
          this.drag.moved = this.drag.moved || JSON.stringify(moved) !== JSON.stringify(this.drag.base);
          this.secs = moved; // live preview; committed (for Undo) on release
          this._refresh();
        } else if (this.drag.type === "edge") {
          const moved = moveEdge(this.drag.base, this.drag.edge, pos, this.maxEnd,
            this.drag.together || ev.shiftKey);
          this.drag.moved = this.drag.moved || JSON.stringify(moved) !== JSON.stringify(this.drag.base);
          this.secs = moved; // live preview; committed (for Undo) on release
          this._refresh();
        } else {
          this._updateAdd(pos);
        }
      },
      pointerup: (ev) => {
        if (ev.button !== 0 || !this.drag) return;
        const d = this.drag;
        this.drag = null;
        if (d.type === "edge" || d.type === "move") {
          if (d.moved) {
            const after = this.secs;
            this.secs = d.base;
            // one Undo step for the whole drag; if a section was deleted, select nothing
            this._apply(after, after.length === d.base.length ? this.selected : -1);
          }
        } else {
          this.pending = null;
          d.to = this.barAt(ev.clientX);
          // the same range the preview showed: from where you pressed to where you let go
          const r = addSection(this.secs, d.from, d.to ?? d.from, this.lastKind, this.maxEnd);
          if (r) return this._apply(r.secs, r.index);
          this._refresh();
        }
      },
      pointercancel: () => {
        if (this.drag?.base) this.secs = this.drag.base;
        this.drag = null;
        this.pending = null;
        this._refresh();
      },
      dblclick: (ev) => {
        const pos = this.barAt(ev.clientX);
        if (findEdge(this.secs, pos, this._tolBars())) return; // that was an edge
        this.splitAtBar(pos);
      },
      // any click elsewhere on the page hands the keyboard back to the timeline
      docdown: (ev) => {
        if (!this.li.contains(ev.target)) this.focused = false;
      },
    };
    for (const name of ["pointerdown", "pointermove", "pointerup", "pointercancel", "dblclick"]) {
      c.addEventListener(name, this._handlers[name]);
    }
    document.addEventListener("pointerdown", this._handlers.docdown, true);
  }

  _unlisten() {
    const c = this.canvas;
    for (const name of ["pointerdown", "pointermove", "pointerup", "pointercancel", "dblclick"]) {
      c.removeEventListener(name, this._handlers[name]);
    }
    document.removeEventListener("pointerdown", this._handlers.docdown, true);
    c.style.cursor = "";
  }

  /** While drawing a new section: preview it from where you started to the pointer. */
  _updateAdd(pos) {
    this.drag.to = pos;
    const r = addSection(this.secs, this.drag.from, pos, this.lastKind, this.maxEnd);
    this.pending = r ? r.secs[r.index] : null;
    this._refresh();
  }

  /**
   * Keyboard shortcuts (app.js passes key presses here first while the editor has the
   * keyboard). Returns true if the key was used.
   */
  handleKey(ev) {
    if (!this.focused || ev.target.closest?.("input, select, textarea")) return false;
    const ctrl = ev.ctrlKey || ev.metaKey;
    const key = ev.key.toLowerCase();
    if (ctrl && key === "z" && !ev.shiftKey) return this.undo(), true;
    if (ctrl && (key === "y" || (key === "z" && ev.shiftKey))) return this.redo(), true;
    if ((ev.key === "Delete" || ev.key === "Backspace") && this.selected >= 0) return this.deleteSelected(), true;
    if (ev.key === "Escape") return this.select(-1), true;
    return false;
  }
}
