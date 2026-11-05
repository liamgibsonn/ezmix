// Section preview: plays a bar range of one track in the browser.
//
// The audio comes from /api/tracks/{id}/audio (the real file, read-only). Bars are
// turned into seconds with the track's `bar_times` (start time of every bar, from the
// Rekordbox beatgrid), so a preview starts exactly on the phrase's first beat.
//
// One preview plays at a time. Starting another stops the current one.
// It can be paused and resumed; when a preview reaches the end of its range it PAUSES
// there (rather than closing), so pressing play again replays it.
// Pausing is how you stop: the player stays open at that spot until something else is
// played. A section preview can also be turned into a normal play-through (playOn).

/**
 * Bar position -> seconds, using track.bar_times (bar_times[0] = start of bar 1).
 * Positions before bar 1 or after the last bar carry on at the nearest bar's length.
 */
export function barToTime(track, bar) {
  const bt = track.bar_times;
  if (!bt || bt.length < 2) return null;
  const i = Math.floor(bar) - 1; // index of the bar we're in
  if (i < 0) return bt[0] + (bar - 1) * (bt[1] - bt[0]);
  if (i >= bt.length - 1) {
    const last = bt.length - 1;
    return bt[last] + (bar - 1 - last) * (bt[last] - bt[last - 1]);
  }
  return bt[i] + (bar - 1 - i) * (bt[i + 1] - bt[i]);
}

/** Seconds -> bar position (inverse of barToTime). */
export function timeToBar(track, t) {
  const bt = track.bar_times;
  if (!bt || bt.length < 2) return 1;
  if (t <= bt[0]) return 1 + (t - bt[0]) / (bt[1] - bt[0]);
  const last = bt.length - 1;
  if (t >= bt[last]) return 1 + last + (t - bt[last]) / (bt[last] - bt[last - 1]);
  // binary search for the bar containing t
  let lo = 0;
  let hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (bt[mid] <= t) lo = mid;
    else hi = mid;
  }
  return 1 + lo + (t - bt[lo]) / (bt[lo + 1] - bt[lo]);
}

// How a preview ends (seconds): fade out over END_FADE, stopping END_EARLY before the
// end bar line. Small enough not to cut the music you want to hear (40 ms is under a
// 16th note even at 174 BPM), big enough that the next section's first hit is never heard.
const END_FADE = 0.06;
const END_EARLY = 0.04;

export class PreviewPlayer {
  /**
   * onUpdate(state) is called whenever something changes and on every animation frame
   * while playing. state = null when stopped (player closed), otherwise
   *   {track, fromBar, toBar, bar, label, source, paused, ended}
   * where `bar` is the current position and `source` is whatever was passed to play()
   * (app.js uses it to know which strip / timeline block to draw a playhead on).
   * onError(message) is called if the audio can't be played.
   */
  constructor({ onUpdate, onError }) {
    this.onUpdate = onUpdate || (() => {});
    this.onError = onError || (() => {});
    this.audio = new Audio();
    this.audio.preload = "auto";
    this.state = null;
    this._frame = null;
    this._token = 0; // increases on every play/stop, so stale async work is ignored
  }

  /** True while something is loaded in the player (playing OR paused). */
  get playing() {
    return this.state !== null;
  }

  /** True if the player is open but paused. */
  get paused() {
    return !!this.state?.paused;
  }

  /** Play bars fromBar..toBar (toBar = position just AFTER the last bar) of `track`. */
  async play(track, fromBar, toBar, label, source = null) {
    this.stop();
    const token = ++this._token;
    const t0 = barToTime(track, fromBar);
    if (t0 === null) return this.onError("This track has no beatgrid, so it can't be previewed.");

    // Load the track's audio if it isn't already loaded. The "?v=2" makes the address
    // new, so browsers can't reuse an MP3 they saved before MP3s were served as WAV
    // copies (2026-10-15; the old MP3 sounded a beat off after jumping around).
    const src = `/api/tracks/${encodeURIComponent(track.id)}/audio?v=2`;
    if (this.audio.dataset.trackId !== track.id) {
      this.audio.dataset.trackId = track.id;
      this.audio.src = src;
    }
    this.state = { track, fromBar, toBar, bar: fromBar, label, source, loading: true, paused: false, ended: false };
    this.onUpdate(this.state);

    try {
      if (this.audio.readyState < 1) await waitFor(this.audio, "loadedmetadata");
      if (token !== this._token) return; // something else started meanwhile
      this.audio.currentTime = Math.max(0, t0);
      await this.audio.play();
    } catch (e) {
      if (token !== this._token) return;
      this.audio.dataset.trackId = ""; // force a reload next time
      this.state = null;
      this.onUpdate(null);
      return this.onError(
        `Couldn't play "${track.title}". The audio file may be missing or in a format ` +
          `your browser can't play.`
      );
    }
    if (token !== this._token) return;
    this.state.loading = false;
    this._tick(token);
  }

  /**
   * Jump to a bar while playing. After a jump, playback carries on to the END of the
   * track (instead of stopping at the end of the section that was previewed), so you
   * can listen through to whatever comes next.
   */
  seek(bar) {
    const s = this.state;
    if (!s) return;
    const end = trackEndBar(s.track);
    const target = Math.max(Math.min(s.track.first_bar ?? 1, 1), Math.min(bar, end - 0.25));
    const t = barToTime(s.track, target);
    if (t === null) return;
    s.toBar = end;
    s.freePlay = true;
    s.bar = target;
    s.ended = false;
    this.audio.currentTime = Math.max(0, t);
    this.onUpdate(s);
  }

  /**
   * Stop treating this as a section preview: drop the "bars x-y" limit and carry on
   * from where it is through to the end of the track (the ✕ on a section preview).
   * Works while playing or paused (then ▶ carries on from the same spot).
   */
  playOn() {
    const s = this.state;
    if (!s || s.freePlay) return;
    s.toBar = trackEndBar(s.track);
    s.freePlay = true;
    s.ended = false; // so ▶ carries on from here instead of replaying the section
    this.onUpdate(s);
  }

  /** Pause, keeping the player open at the current position. */
  pause() {
    const s = this.state;
    if (!s || s.paused) return;
    this._token++; // stops the frame loop
    cancelAnimationFrame(this._frame);
    this.audio.pause();
    s.paused = true;
    this.onUpdate(s);
  }

  /** Carry on playing. If the preview had finished, start it again from its start. */
  async resume() {
    const s = this.state;
    if (!s || !s.paused) return;
    if (s.ended) {
      s.bar = s.fromBar;
      s.ended = false;
      this.audio.currentTime = Math.max(0, barToTime(s.track, s.fromBar) ?? 0);
    }
    const token = ++this._token;
    try {
      await this.audio.play();
    } catch {
      return this.onError(`Couldn't resume "${s.track.title}".`);
    }
    if (token !== this._token) return;
    s.paused = false;
    this._tick(token);
  }

  /** Play/pause toggle (the button at the bottom, and the Space key). */
  toggle() {
    if (this.paused) this.resume();
    else this.pause();
  }

  /** Jump forwards/backwards by a number of bars (e.g. -8 / +8). */
  skip(bars) {
    if (this.state) this.seek(this.state.bar + bars);
  }

  /** Stop playback and close the player (safe to call when nothing is playing). */
  stop() {
    this._token++;
    cancelAnimationFrame(this._frame);
    if (!this.audio.paused) this.audio.pause();
    if (this.state) {
      this.state = null;
      this.onUpdate(null);
    }
  }

  /**
   * Every animation frame: update the position; pause at the end of the range.
   * The end is CLEAN: the volume dips out over the last END_FADE seconds and playback
   * pauses END_EARLY seconds before the boundary. Without this, the start of the next
   * section (its first kick sits right on, or a few ms before, the grid line) could be
   * heard before the cut: frames come ~16 ms apart and pausing isn't instant (the user
   * heard it on T12, 2026-10-15).
   */
  _tick(token) {
    if (token !== this._token || !this.state) return;
    const now = this.audio.currentTime;
    const bar = timeToBar(this.state.track, now);
    const tEnd = barToTime(this.state.track, this.state.toBar);
    const left = tEnd === null ? Infinity : tEnd - now; // seconds to the end of the range
    if (left <= END_EARLY || this.audio.ended) {
      this.state.bar = this.state.toBar;
      this.state.ended = true;
      this.pause();
      this.audio.volume = 1; // ready for the next play (we're paused, so it's silent)
      return;
    }
    this.audio.volume = Math.min(1, Math.max(0, (left - END_EARLY) / END_FADE));
    this.state.bar = bar;
    this.onUpdate(this.state);
    this._frame = requestAnimationFrame(() => this._tick(token));
  }
}

/** Bar position just after the track's last complete bar. */
export function trackEndBar(track) {
  return (track.bar_count || 0) + 1;
}

/** Resolve when `target` fires `event`; reject on an "error" event. */
function waitFor(target, event) {
  return new Promise((resolve, reject) => {
    const ok = () => {
      cleanup();
      resolve();
    };
    const fail = () => {
      cleanup();
      reject(new Error("audio error"));
    };
    const cleanup = () => {
      target.removeEventListener(event, ok);
      target.removeEventListener("error", fail);
    };
    target.addEventListener(event, ok);
    target.addEventListener("error", fail);
  });
}
