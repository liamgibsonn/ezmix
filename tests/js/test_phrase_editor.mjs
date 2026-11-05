// Tests for the phrase editor's editing logic (web/phrase-editor.js, pure functions).
// Run: node --test tests/js   (also run by tests/test_web_js.py as part of pytest)

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  addAfter, addSection, findEdge, fromSaved, mergeWith, midBar, moveEdge, moveSection,
  nextKindAfter, numberedLabels, removeAt, sectionAt, setKind, splitAt, toPhrases, toSaved,
} from "../../web/phrase-editor.js";

// intro 1-16 | build 17-24 | drop 25-56 | (gap 57-64) | outro 65-72   (track: 72 bars)
const SAVED = [["Intro", 1, 16], ["Build", 17, 24], ["Drop", 25, 56], ["Outro", 65, 72]];
const MAX_END = 73;
const secs = () => fromSaved(SAVED);

test("saved format round trip (last bar included <-> bar after)", () => {
  assert.deepEqual(secs()[1], { kind: "build", start: 17, end: 25 });
  assert.deepEqual(toSaved(secs()), SAVED);
  assert.deepEqual(fromSaved([["Pre-drop", 5, 6]]), [{ kind: "predrop", start: 5, end: 7 }]);
});

test("findEdge finds shared edges and edges next to gaps", () => {
  assert.deepEqual(findEdge(secs(), 17.2, 0.5), { bar: 17, left: 0, right: 1 });
  assert.deepEqual(findEdge(secs(), 57.1, 0.5), { bar: 57, left: 2, right: -1 });
  assert.equal(findEdge(secs(), 20, 0.5), null);
});

test("a shared edge: only ONE section moves (drag left = left one, right = right one)", () => {
  const e = findEdge(secs(), 17, 0.5);
  // drag left to 12.6 -> the intro ends at 13 (bars 1-12), a gap opens, the build stays
  assert.deepEqual(toSaved(moveEdge(secs(), e, 12.6, MAX_END)).slice(0, 2), [["Intro", 1, 12], ["Build", 17, 24]]);
  // drag right to 20.6 -> the build starts at 21, the intro stays
  assert.deepEqual(toSaved(moveEdge(secs(), e, 20.6, MAX_END)).slice(0, 2), [["Intro", 1, 16], ["Build", 21, 24]]);
});

test("Shift+drag (together) moves a shared edge for both", () => {
  const out = moveEdge(secs(), findEdge(secs(), 17, 0.5), 20.6, MAX_END, true);
  assert.deepEqual(toSaved(out).slice(0, 2), [["Intro", 1, 20], ["Build", 21, 24]]);
});

test("drag an edge right across its section to delete it (even 1-bar ones)", () => {
  const e = findEdge(secs(), 17, 0.5);
  assert.deepEqual(toSaved(moveEdge(secs(), e, 26, MAX_END)).map((s) => s[0]), ["Intro", "Drop", "Outro"]);
  assert.deepEqual(toSaved(moveEdge(secs(), e, -5, MAX_END)).map((s) => s[0]), ["Build", "Drop", "Outro"]);
  const one = fromSaved([["Build", 1, 8], ["Pre-drop", 9, 9], ["Drop", 10, 20]]);
  assert.deepEqual(toSaved(moveEdge(one, findEdge(one, 10, 0.5), 9, 21)).map((s) => s[0]), ["Build", "Drop"]);
});

test("an edge next to a gap grows into it, but stops at the next section / track end", () => {
  const g = findEdge(secs(), 57, 0.5);
  assert.deepEqual(toSaved(moveEdge(secs(), g, 70, MAX_END))[2], ["Drop", 25, 64]);
  const last = findEdge(secs(), 73, 0.5);
  assert.deepEqual(toSaved(moveEdge(secs(), last, 99, MAX_END))[3], ["Outro", 65, 72]);
  const o = findEdge(secs(), 65, 0.5); // outro's start, gap before it
  assert.deepEqual(toSaved(moveEdge(secs(), o, 50, MAX_END))[3], ["Outro", 57, 72]);
});

test("slide a section into the gap next to it; it stops at neighbours", () => {
  // the outro (65-72) can slide left into the gap 57-64, but not past the drop
  assert.deepEqual(toSaved(moveSection(secs(), 3, -5.4, MAX_END))[3], ["Outro", 60, 67]);
  assert.deepEqual(toSaved(moveSection(secs(), 3, -30, MAX_END))[3], ["Outro", 57, 64]);
  // no gap on the right (track end): stays
  assert.deepEqual(toSaved(moveSection(secs(), 3, 4, MAX_END))[3], ["Outro", 65, 72]);
  // the build is boxed in: stays
  assert.deepEqual(toSaved(moveSection(secs(), 1, 3, MAX_END))[1], ["Build", 17, 24]);
});

test("split in half: the middle bar", () => {
  assert.equal(midBar(secs(), 1), 21); // 17-24 -> 17-20 + 21-24
  assert.equal(midBar(fromSaved([["Build", 1, 7]]), 0), 5); // 7 bars -> 4 + 3
  assert.equal(midBar(fromSaved([["Build", 1, 1]]), 0), null); // 1 bar can't split
});

test("+ Add after: into the gap, or takes the last bar", () => {
  // drop 25-56 has a gap after it: new 1-bar section at 57
  let r = addAfter(secs(), 2, "breakdown", MAX_END);
  assert.deepEqual(toSaved(r.secs)[3], ["Breakdown", 57, 57]);
  assert.equal(r.index, 3);
  // build 17-24 touches the drop: takes the build's last bar -> pre-drop at 24
  r = addAfter(secs(), 1, nextKindAfter("build"), MAX_END);
  assert.deepEqual(toSaved(r.secs).slice(1, 4), [["Build", 17, 23], ["Pre-drop", 24, 24], ["Drop", 25, 56]]);
  // a 1-bar section with no room after it: nothing
  const one = fromSaved([["Pre-drop", 9, 9], ["Drop", 10, 20]]);
  assert.equal(addAfter(one, 0, "drop", 21), null);
  assert.equal(nextKindAfter("predrop"), "drop");
  assert.equal(nextKindAfter("drop"), "build");
});

test("moveEdge never changes the list it was given", () => {
  const before = secs();
  const copy = JSON.stringify(before);
  moveEdge(before, findEdge(before, 17, 0.5), 20, MAX_END);
  assert.equal(JSON.stringify(before), copy);
});

test("split at a bar", () => {
  const r = splitAt(secs(), 41);
  assert.equal(r.index, 3);
  assert.deepEqual(toSaved(r.secs).slice(2, 4), [["Drop", 25, 40], ["Drop", 41, 56]]);
  assert.equal(splitAt(secs(), 25), null); // already an edge
  assert.equal(splitAt(secs(), 60), null); // in the gap
});

test("merge keeps the selected section's type; not across a gap", () => {
  const r = mergeWith(secs(), 1, -1); // build merged with intro before it
  assert.deepEqual(toSaved(r.secs)[0], ["Build", 1, 24]);
  assert.equal(r.index, 0);
  assert.deepEqual(toSaved(mergeWith(secs(), 1, 1).secs)[1], ["Build", 17, 56]);
  assert.equal(mergeWith(secs(), 2, 1), null); // drop | gap | outro
  assert.equal(mergeWith(secs(), 0, -1), null); // nothing before the intro
});

test("set type, remove", () => {
  assert.equal(setKind(secs(), 1, "groove")[1].kind, "groove");
  assert.equal(setKind(secs(), 1, "chorus")[1].kind, "build"); // not one of ours: ignored
  assert.deepEqual(toSaved(removeAt(secs(), 1)).map((s) => s[0]), ["Intro", "Drop", "Outro"]);
});

test("adding a section fits it into the gap you started in", () => {
  const r = addSection(secs(), 58.3, 80, "breakdown", MAX_END);
  assert.deepEqual(toSaved(r.secs)[3], ["Breakdown", 58, 64]); // cut off at the outro
  assert.equal(r.index, 3);
  // dragged backwards: from 63 back to 50 -> starts where the drop ends
  assert.deepEqual(toSaved(addSection(secs(), 63.5, 50, "groove", MAX_END).secs)[3], ["Groove", 57, 63]);
  // starting inside a section does nothing
  assert.equal(addSection(secs(), 30, 60, "drop", MAX_END), null);
  // a blank track
  assert.deepEqual(toSaved(addSection([], 1.2, 16.9, "intro", MAX_END).secs), [["Intro", 1, 16]]);
});

test("labels are numbered like the Auto (EDM) detection", () => {
  const s = fromSaved([["Intro", 1, 8], ["Drop", 9, 16], ["Drop", 17, 24], ["Breakdown", 25, 32],
    ["Drop", 33, 40], ["Drop", 49, 56], ["Outro", 57, 60], ["Outro", 61, 64]]);
  assert.deepEqual(numberedLabels(s), ["Intro", "Drop 1", "Drop 1", "Breakdown", "Drop 2", "Drop 3", "Outro", "Outro"]);
});

test("toPhrases gives what the strip drawing needs", () => {
  const p = toPhrases(secs())[2];
  assert.deepEqual(p, { kind: "drop", label: "Drop", start_bar: 25, length_bars: 32, end_bar: 57,
    fill_start_bar: null, source: "custom" });
});

test("sectionAt", () => {
  assert.equal(sectionAt(secs(), 24.9), 1);
  assert.equal(sectionAt(secs(), 25), 2);
  assert.equal(sectionAt(secs(), 60), -1);
});
