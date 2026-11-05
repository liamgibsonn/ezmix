// Cheat sheet page: shows the solved plan as bar-by-bar steps (one transition at a
// time, done on the fly: no "prep before the set" list).
// Open as cheatsheet.html?plan=<plan id> (the planner's "Cheat sheet" button does this).
//
// Ticks (steps done) are remembered in this browser only, per plan, so
// you can tick things off while practising. "Clear ticks" resets them.

const params = new URLSearchParams(location.search);
const planId = params.get("plan");
const TICKS_KEY = `djplanner.ticks.${planId}`;

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

// ----- ticks (browser storage, wrapped in try: storage can be unavailable) -----------

function loadTicks() {
  try {
    return new Set(JSON.parse(localStorage.getItem(TICKS_KEY)) || []);
  } catch {
    return new Set();
  }
}

function saveTicks(ticks) {
  try {
    localStorage.setItem(TICKS_KEY, JSON.stringify([...ticks]));
  } catch {
    /* not saved; ticks just won't survive a reload */
  }
}

const ticks = loadTicks();

function toggleTick(key, on) {
  if (on) ticks.add(key);
  else ticks.delete(key);
  saveTicks(ticks);
}

// ----- rendering -----------------------------------------------------------------------

function deckBadge(deck) {
  return el("span", `deck-badge deck-${deck}`, `Deck ${deck}`);
}

function renderSheet(sheet) {
  document.title = `${sheet.name} · Cheat Sheet`;
  $("sheet-name").textContent = sheet.name;
  $("sheet-meta").textContent = sheet.bpm
    ? `${sheet.total_bars} bars · ${sheet.bpm} BPM · ` +
      `${sheet.steps.filter((s) => s.kind !== "load").length} actions`
    : `${sheet.total_bars} bars`;

  // Problems
  $("problems").hidden = !sheet.issues.length;
  $("problem-list").replaceChildren(
    ...sheet.issues.map((i) => el("li", `lvl-${i.level}`, i.message))
  );

  // Which tracks go on which deck (you decide when to load them)
  $("decks").replaceChildren(
    ...(sheet.decks || []).map((d) => {
      // Same columns as the steps below, so the deck badges line up: the side of the
      // controller goes where the bar number is, then the deck and its track(s).
      const li = el("li", "deck-row");
      li.style.setProperty("--deck-color", `var(--deck-${d.deck})`);
      li.append(el("div", "deck-side", d.side ? `${d.side} side` : ""));
      const body = el("div", "deck-body");
      body.append(deckBadge(d.deck));
      const names = el("span", "deck-tracks");
      d.tracks.forEach((t, k) => {
        if (k) names.append(el("span", "muted", ` then `));
        names.append(el("strong", null, t.title));
        if (k) names.append(el("span", "muted", ` (from ${t.from_bar})`));
      });
      body.append(names);
      li.append(body);
      return li;
    })
  );

  // Steps: bar first, then the deck, then a SHORT instruction, with the details small
  // underneath. A track's get-ready steps are indented and share its deck colour.
  $("steps").replaceChildren(...sheet.steps.map((s, i) => stepItem(s, i)));
}

/** One step of the cheat sheet (click to tick it off). */
function stepItem(s, i) {
      const key = `step:${i}`;
      const li = el("li", `step kind-${s.kind}`);
      if (s.group != null) li.classList.add("in-group");
      li.style.setProperty("--deck-color", `var(--deck-${s.deck})`);
      if (ticks.has(key)) li.classList.add("done");

      const when = el("div", "step-when");
      // Everything in bars (no clock times). Getting ready before ANYTHING starts (bar
      // 1.1, e.g. loading the first track) is obvious, so no bar number for that.
      const untimed = s.when === "by" && s.set_bar <= 1;
      // "any time" steps show the bar they have to be done before (the end of the window)
      const barText = untimed ? "" : s.when === "between" && s.before ? s.before : `${s.bar}.${s.beat}`;
      when.append(
        el("div", "step-bar", barText),
        el("div", "step-time", untimed ? "" : s.when === "between" || s.when === "by" ? "before" : "")
      );
      const body = el("div", "step-body");
      const main = el("div", "step-main");
      main.append(deckBadge(s.deck), el("span", "step-text", s.short || s.text));
      if (s.flags.includes("awkward")) main.append(el("span", "awkward", "⚠ too tight"));
      else if (s.flags.includes("busy")) main.append(el("span", "busy", "busy"));
      body.append(main);
      if (s.detail) body.append(el("div", "step-detail", s.detail));
      li.append(when, body);
      li.title = s.text; // the full sentence, on hover

      li.addEventListener("click", () => {
        const on = !li.classList.contains("done");
        li.classList.toggle("done", on);
        toggleTick(key, on);
      });
      return li;
}

function showError(msg) {
  $("error").hidden = false;
  $("error").textContent = msg;
}

// ----- start --------------------------------------------------------------------------

$("print-btn").addEventListener("click", () => window.print());
$("reset-btn").addEventListener("click", () => {
  ticks.clear();
  saveTicks(ticks);
  document.querySelectorAll(".step.done").forEach((s) => s.classList.remove("done"));
});

if (!planId) {
  showError("No set chosen. Open the cheat sheet from the planner's “Cheat sheet” button.");
} else {
  fetch(`/api/plans/${encodeURIComponent(planId)}/cheatsheet`)
    .then(async (r) => {
      const data = await r.json().catch(() => null);
      if (!r.ok) throw new Error((data && data.detail) || r.statusText);
      renderSheet(data);
    })
    .catch((e) => showError(`Couldn't load the cheat sheet: ${e.message}`));
}
