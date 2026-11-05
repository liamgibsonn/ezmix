// Draws a track's "phrase strip" on a <canvas>:
//
//   ┌────────────────────────────────────────────────────────────┐
//   │ 1        17        33        49  ...      ← bar ruler        │
//   │ A                  B                       ← cue flags       │
//   │[ Intro ][ Up ][  Chorus 1  ][ Chorus 2 ]... ← coloured blocks │
//   │ ▁▃▅▇▅▃▁▃▅▇▇▅▃ ...                          ← waveform on top │
//   └────────────────────────────────────────────────────────────┘
//
// The horizontal axis is BARS (not seconds), matching the rest of the app.
// Kept separate from app.js so the Phase 2 timeline can reuse it.

/** Phrase categories in display order, with the legend label for each. */
export const PHRASE_KINDS = {
  // our EDM detection (phrase source "detected")
  detected: [
    ["intro", "Intro"],
    ["build", "Build"],
    ["predrop", "Pre-drop"],
    ["fakedrop", "Fake drop"],
    ["drop", "Drop"],
    ["groove", "Groove"],
    ["breakdown", "Breakdown"],
    ["outro", "Outro"],
  ],
  // Rekordbox's phrase analysis
  rekordbox: [
    ["intro", "Intro"],
    ["up", "Up"],
    ["down", "Down"],
    ["chorus", "Chorus"],
    ["verse", "Verse"],
    ["bridge", "Bridge"],
    ["outro", "Outro"],
    ["bars", "Plain 16-bar blocks"],
  ],
  // your own sections, made in the phrase editor (same types as our EDM detection)
  custom: [
    ["intro", "Intro"],
    ["build", "Build"],
    ["predrop", "Pre-drop"],
    ["fakedrop", "Fake drop"],
    ["drop", "Drop"],
    ["groove", "Groove"],
    ["breakdown", "Breakdown"],
    ["outro", "Outro"],
  ],
};

/** Read a CSS colour variable from style.css, e.g. cssVar("--phrase-intro"). */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Colour for a phrase category (falls back to --phrase-unknown). */
export function phraseColor(kind) {
  return cssVar(`--phrase-${kind}`) || cssVar("--phrase-unknown");
}

/**
 * Visible bar range for a track: from its first grid beat (or bar 1) to the end of the
 * last phrase/bar. Returns {start, end} in bar positions.
 */
export function barRange(track) {
  const start = Math.min(track.first_bar ?? 1, 1);
  const lastPhraseEnd = Math.max(1, ...track.phrases.map((p) => p.end_bar));
  const end = Math.max(track.bar_count + 1, lastPhraseEnd);
  return { start, end };
}

/** Layout constants (CSS pixels). */
const RULER_H = 14; // height of the bar-number ruler at the top
const FILL_H = 3; // height of the "fill" marker line at the bottom of a phrase

/**
 * Draw `track` (the JSON from /api/tracks/{id}) onto `canvas`.
 * Safe to call again whenever the canvas changes size.
 * options.highlight: a phrase to outline (the one under the mouse), or null.
 * options.playRange: [fromBar, toBar] being previewed (tinted), or null.
 * options.playhead:  current preview position in bars (white line), or null.
 * options.emptyText: message to show when the track has no sections (Custom), or null.
 */
export function drawPhraseStrip(canvas, track, options = {}) {
  // --- size the canvas for sharp drawing on high-DPI screens ---
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // draw in CSS pixels from here on
  ctx.clearRect(0, 0, w, h);

  const { start, end } = barRange(track);
  // x position (pixels) of a bar position
  const xOf = (bar) => ((bar - start) / (end - start)) * w;
  const bandTop = RULER_H;
  const bandH = h - RULER_H;

  // --- phrase blocks ---
  ctx.font = "600 11px system-ui, sans-serif";
  ctx.textBaseline = "top";
  // The first phrase's colour also covers any beats BEFORE it at the very start (e.g. a
  // grid that begins a few beats before bar 1, as on tracks with a tempo change), so
  // there's no blank gap. Its start bar - what you click, hear and add - doesn't change.
  if (!track.phrases.length && options.emptyText) {
    ctx.fillStyle = cssVar("--strip-ruler");
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillText(options.emptyText, 8, bandTop + 4);
    ctx.font = "600 11px system-ui, sans-serif";
  }
  const firstStart = Math.min(...track.phrases.map((p) => p.start_bar));
  for (const p of track.phrases) {
    if (p.start_bar === firstStart && start < p.start_bar) {
      ctx.fillStyle = phraseColor(p.kind);
      ctx.fillRect(0, bandTop, xOf(p.start_bar), bandH);
    }
    const x0 = xOf(p.start_bar);
    const x1 = xOf(p.end_bar);
    ctx.fillStyle = phraseColor(p.kind);
    ctx.fillRect(x0, bandTop, x1 - x0, bandH);

    // thin dark divider at the start of each phrase
    ctx.fillStyle = cssVar("--strip-divider");
    ctx.fillRect(Math.round(x0), bandTop, 1, bandH);

    // fill-in marker: a light bar along the bottom from where the fill starts
    if (p.fill_start_bar != null) {
      ctx.fillStyle = "rgba(255,255,255,0.75)";
      const fx = xOf(p.fill_start_bar);
      ctx.fillRect(fx, h - FILL_H, x1 - fx, FILL_H);
    }

    // label, only if it fits inside the block
    const label = p.label;
    const tw = ctx.measureText(label).width;
    if (tw + 8 < x1 - x0) {
      ctx.fillStyle = cssVar("--strip-label");
      ctx.fillText(label, x0 + 4, bandTop + 3);
    }
  }

  // --- waveform: one bar per beat, drawn up from the bottom of the band ---
  if (track.waveform.length) {
    ctx.fillStyle = cssVar("--strip-wave");
    const beatW = xOf(start + 0.25) - xOf(start); // width of one beat in px
    const waveH = bandH - 16; // leave room for the label row
    track.waveform.forEach((v, i) => {
      const bar = track.first_bar + i / 4; // beat i -> bar position
      const bh = v * waveH;
      ctx.fillRect(xOf(bar), h - bh, Math.max(beatW - 0.3, 0.6), bh);
    });
  }

  // --- bar ruler: numbers every 8 or 16 bars depending on space ---
  ctx.fillStyle = cssVar("--strip-ruler");
  ctx.font = "10px system-ui, sans-serif";
  const pxPerBar = w / (end - start);
  const step = pxPerBar * 8 >= 28 ? 8 : pxPerBar * 16 >= 28 ? 16 : 32;
  for (let bar = 1; bar < end; bar += step) {
    const x = xOf(bar);
    ctx.fillRect(Math.round(x), 0, 1, 4);
    ctx.fillText(String(bar), x + 2, 2);
  }

  // --- highlighted phrase: bright outline + slight lightening ---
  if (options.highlight) {
    const x0 = xOf(options.highlight.start_bar);
    const x1 = xOf(options.highlight.end_bar);
    ctx.fillStyle = "rgba(255,255,255,0.12)";
    ctx.fillRect(x0, bandTop, x1 - x0, bandH);
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 2;
    ctx.strokeRect(x0 + 1, bandTop + 1, x1 - x0 - 2, bandH - 2);
  }

  // --- preview: tint the range being played, and draw the playhead ---
  if (options.playRange) {
    const [a, b] = options.playRange;
    ctx.fillStyle = "rgba(79,179,255,0.22)";
    ctx.fillRect(xOf(a), bandTop, xOf(b) - xOf(a), bandH);
  }
  if (options.playhead != null) {
    ctx.fillStyle = "#fff";
    ctx.fillRect(Math.round(xOf(options.playhead)) - 1, 0, 2, h);
  }

  // --- cues: vertical line + pad letter ("M" for memory cues) ---
  for (const c of track.cues) {
    const x = xOf(c.bar);
    const color = c.type === "hot" ? cssVar("--cue-hot") : cssVar("--cue-memory");
    // saved loop: shade the loop region
    if (c.loop_end_bar != null) {
      ctx.fillStyle = color + "55";
      ctx.fillRect(x, bandTop, xOf(c.loop_end_bar) - x, bandH);
    }
    ctx.fillStyle = color;
    ctx.fillRect(Math.round(x), bandTop, 1.5, bandH);
    const tag = c.type === "hot" ? c.pad || "?" : "M";
    ctx.fillRect(x, bandTop, 12, 12);
    ctx.fillStyle = "#000";
    ctx.font = "700 10px system-ui, sans-serif";
    ctx.fillText(tag, x + 3, bandTop + 1);
  }
}

/**
 * Work out what's under the mouse. Returns {bar, beat, phrase, position} where
 * bar/beat are the human numbers ("bar 33, beat 2"), position is the exact bar
 * position, and phrase is the phrase object (or null).
 */
export function hitTest(canvas, track, clientX) {
  const rect = canvas.getBoundingClientRect();
  const { start, end } = barRange(track);
  const position = start + ((clientX - rect.left) / rect.width) * (end - start);
  // Which beat the mouse is INSIDE (rounds down), counting beats from bar 1 beat 1.
  const totalBeats = Math.floor((position - 1) * 4);
  const bar = Math.floor(totalBeats / 4) + 1;
  const beat = (((totalBeats % 4) + 4) % 4) + 1;
  let phrase = track.phrases.find((p) => position >= p.start_bar && position < p.end_bar) || null;
  // beats before the first phrase are drawn as part of it, so they act as part of it too
  if (!phrase && track.phrases.length) {
    const first = track.phrases.reduce((a, p) => (p.start_bar < a.start_bar ? p : a));
    if (position < first.start_bar) phrase = first;
  }
  return { bar, beat, phrase, position };
}
