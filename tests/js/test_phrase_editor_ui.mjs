// Simulated use of the PhraseEditor class (web/phrase-editor.js) with a tiny fake page
// (no browser here): drags, clicks, double-clicks and keys go in, and we check the
// sections it would save. What it LOOKS like isn't checked: that needs a real browser.

import assert from "node:assert/strict";
import { test } from "node:test";

// ----- a minimal fake DOM: just what the editor uses -----------------------------------
class FakeEl {
  constructor(tag) {
    Object.assign(this, { tag, children: [], parent: null, listeners: {}, style: {}, value: "",
      disabled: false, textContent: "", className: "", title: "" });
    const cls = new Set();
    this.classList = {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
      toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)),
    };
  }
  append(...kids) { for (const k of kids) if (typeof k === "object") { k.parent = this; this.children.push(k); } }
  after(el) { el.parent = this.parent; this.parent.children.push(el); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  contains(el) { for (let e = el; e; e = e.parent) if (e === this) return true; return false; }
  closest(sel) { for (let e = this; e; e = e.parent) if (sel.split(",").map((s) => s.trim()).includes(e.tag)) return e; return null; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
  fire(t, ev = {}) { for (const fn of this.listeners[t] || []) fn({ target: this, stopPropagation() {}, ...ev }); }
  setPointerCapture() {}
  getBoundingClientRect() { return { left: 0, width: 730 }; }
  // find a child by text (buttons)
  find(text) {
    for (const c of this.children) {
      if (c.textContent === text) return c;
      const f = c.find(text);
      if (f) return f;
    }
    return null;
  }
}
const docListeners = {};
globalThis.document = {
  createElement: (t) => new FakeEl(t),
  addEventListener: (t, fn) => (docListeners[t] = fn),
  removeEventListener: (t) => delete docListeners[t],
};
globalThis.confirm = () => true;

const { PhraseEditor } = await import("../../web/phrase-editor.js");

// The strip: 730 px wide showing bars 1..73 (a 72-bar track) -> 10 px per bar, bar = 1 + x/10
const xOf = (bar) => (bar - 1) * 10;
function makeEditor(sections = [], { onSave } = {}) {
  const li = new FakeEl("li");
  const canvas = new FakeEl("canvas");
  li.append(canvas);
  let saved = null;
  let closed = null;
  const ed = new PhraseEditor({
    li, canvas,
    track: { id: "t1", bar_count: 72, phrases: [] },
    entry: { sections, note: "", based_on: "blank" },
    barAt: (x) => 1 + x / 10,
    onChange: () => {},
    onSave: onSave || (async (body) => { saved = body; }),
    getDetected: async () => [["Intro", 1, 32], ["Drop", 33, 72]],
    onClose: (s) => { closed = s; },
  });
  return { ed, li, canvas, saved: () => saved, closed: () => closed };
}
const L = { button: 0, pointerId: 1 };
const drag = (canvas, fromBar, toBar) => {
  canvas.fire("pointerdown", { ...L, clientX: xOf(fromBar) });
  canvas.fire("pointermove", { ...L, clientX: xOf((fromBar + toBar) / 2) });
  canvas.fire("pointermove", { ...L, clientX: xOf(toBar) });
  canvas.fire("pointerup", { ...L, clientX: xOf(toBar) });
};
const click = (canvas, bar) => {
  canvas.fire("pointerdown", { ...L, clientX: xOf(bar) });
  canvas.fire("pointerup", { ...L, clientX: xOf(bar) });
};

test("blank track: draw sections, set their types, save", async () => {
  const { ed, li, canvas, saved, closed } = makeEditor();
  drag(canvas, 1.5, 16.5); // bars 1-16
  assert.deepEqual(ed.secs, [{ kind: "intro", start: 1, end: 17 }]);
  assert.equal(ed.selected, 0); // a new section is selected
  // starting right next to the intro's end would grab that edge (and stretch the intro),
  // so start in the empty part and drag back towards it: bars 17-40
  drag(canvas, 40.5, 17.2);
  ed.kindSelect.value = "drop";
  ed.kindSelect.fire("change");
  assert.equal(ed.secs[1].kind, "drop");
  assert.ok(ed.dirty);
  ed.noteInput.value = "test note";
  await ed.save();
  assert.deepEqual(saved(), { sections: [["Intro", 1, 16], ["Drop", 17, 40]], note: "test note", based_on: "blank" });
  assert.equal(closed(), true);
  assert.equal(li.children.length, 1); // toolbar removed
});

test("drag a shared edge right: only the right section moves; undo and redo it", () => {
  const { ed, canvas } = makeEditor([["Intro", 1, 16], ["Drop", 17, 72]]);
  drag(canvas, 17, 21.3); // grab the edge at bar 17, drop it at 21
  assert.deepEqual(ed.secs.map((s) => [s.start, s.end]), [[1, 17], [21, 73]]);
  assert.equal(ed.undoStack.length, 1); // the whole drag = one undo step
  ed.undo();
  assert.deepEqual(ed.secs.map((s) => [s.start, s.end]), [[1, 17], [17, 73]]);
  ed.redo();
  assert.deepEqual(ed.secs.map((s) => [s.start, s.end]), [[1, 17], [21, 73]]);
});

test("Shift+drag moves both touching sections", () => {
  const { ed, canvas } = makeEditor([["Intro", 1, 16], ["Drop", 17, 72]]);
  canvas.fire("pointerdown", { ...L, clientX: xOf(17), shiftKey: true });
  canvas.fire("pointermove", { ...L, clientX: xOf(21), shiftKey: true });
  canvas.fire("pointerup", { ...L, clientX: xOf(21), shiftKey: true });
  assert.deepEqual(ed.secs.map((s) => [s.start, s.end]), [[1, 21], [21, 73]]);
});

test("drag a section's middle to slide it into a gap", () => {
  const { ed, canvas } = makeEditor([["Intro", 1, 8], ["Pre-drop", 12, 12], ["Drop", 17, 72]]);
  drag(canvas, 12.5, 15.5); // slide the 1-bar pre-drop right by 3 bars
  assert.deepEqual(ed.secs[1], { kind: "predrop", start: 15, end: 16 });
  drag(canvas, 15.5, 40); // can't go past the drop: stops touching it
  assert.deepEqual(ed.secs[1], { kind: "predrop", start: 16, end: 17 });
  assert.equal(ed.selected, 1);
});

test("dragging an edge across a section deletes it", () => {
  const { ed, canvas } = makeEditor([["Build", 1, 8], ["Pre-drop", 9, 9], ["Drop", 10, 72]]);
  drag(canvas, 10, 8.6); // drop's start... dragged left: shrinks the pre-drop to nothing
  assert.deepEqual(ed.secs.map((s) => s.kind), ["build", "drop"]);
  ed.undo();
  assert.equal(ed.secs.length, 3);
});

test("Split in half and + Add after buttons", () => {
  const { ed, canvas } = makeEditor([["Build", 1, 8], ["Drop", 9, 72]]);
  click(canvas, 4);
  ed.splitBtn.fire("click");
  assert.deepEqual(ed.secs.map((s) => [s.kind, s.start, s.end - 1]), [["build", 1, 4], ["build", 5, 8], ["drop", 9, 72]]);
  ed.select(1);
  ed.addAfterBtn.fire("click"); // build 5-8 touches the drop: takes bar 8 as a pre-drop
  assert.deepEqual(ed.secs.map((s) => [s.kind, s.start, s.end - 1]),
    [["build", 1, 4], ["build", 5, 7], ["predrop", 8, 8], ["drop", 9, 72]]);
  assert.equal(ed.selected, 2);
  assert.equal(ed.kindSelect.value, "predrop");
});

test("double-click splits at that bar; merge joins back; Delete key removes", () => {
  const { ed, canvas } = makeEditor([["Intro", 1, 16], ["Drop", 17, 72]]);
  canvas.fire("dblclick", { clientX: xOf(49.2) });
  assert.deepEqual(ed.secs.map((s) => [s.kind, s.start, s.end]), [["intro", 1, 17], ["drop", 17, 49], ["drop", 49, 73]]);
  assert.equal(ed.selected, 2);
  ed.mergePrevBtn.fire("click");
  assert.equal(ed.secs.length, 2);
  click(canvas, 5);
  assert.equal(ed.selected, 0);
  const key = { key: "Delete", target: new FakeEl("canvas") };
  assert.equal(ed.handleKey(key), true);
  assert.deepEqual(ed.secs.map((s) => s.kind), ["drop"]);
  // Ctrl+Z brings it back
  assert.equal(ed.handleKey({ key: "z", ctrlKey: true, target: new FakeEl("canvas") }), true);
  assert.equal(ed.secs.length, 2);
});

test("keys go to the timeline after you click elsewhere on the page", () => {
  const { ed } = makeEditor([["Intro", 1, 16]]);
  docListeners.pointerdown({ target: new FakeEl("div") }); // a click outside this card
  assert.equal(ed.handleKey({ key: "z", ctrlKey: true, target: new FakeEl("body") }), false);
});

test("typing in the note box doesn't trigger shortcuts", () => {
  const { ed } = makeEditor([["Intro", 1, 16]]);
  ed.selected = 0;
  assert.equal(ed.handleKey({ key: "Backspace", target: ed.noteInput }), false);
  assert.equal(ed.secs.length, 1);
});

test("Reset to Auto (EDM) loads the detector's sections (undoable)", async () => {
  const { ed } = makeEditor([["Groove", 1, 8]]);
  await ed.resetToDetected();
  assert.deepEqual(ed.secs.map((s) => s.kind), ["intro", "drop"]);
  assert.equal(ed.basedOn, "detected");
  ed.undo();
  assert.deepEqual(ed.secs.map((s) => s.kind), ["groove"]);
});

test("a failed save keeps the editor open and says why", async () => {
  const { ed, closed } = makeEditor([["Intro", 1, 8]], { onSave: async () => { throw new Error("Sections overlap"); } });
  await ed.save();
  assert.equal(closed(), null);
  assert.match(ed.statusText.textContent, /Not saved: Sections overlap/);
  assert.ok(ed.statusText.classList.contains("error"));
});

test("cancel closes without saving", () => {
  const { ed, closed } = makeEditor([["Intro", 1, 8]]);
  ed.cancel();
  assert.equal(closed(), false);
});
