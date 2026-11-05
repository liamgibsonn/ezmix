# DJ Set Planner (for Rekordbox)

Pick sections (intro, chorus, drop…) from tracks in your Rekordbox library, put them
in the order you want, and the planner works out how to perform it on the decks (hot
cue jumps, loops, beat jumps, mixes). It then gives you a bar-by-bar cheat sheet, and
(planned) cues you can import into Rekordbox.

## Features by phase

✅ done · 🟡 in progress · ⬜ planned

**Phase 1: Reader** ✅
- ✅ Reads your Rekordbox library and each track's phrases
- ✅ Works without phrase analysis (graceful fallback)

**Phase 2: Timeline** ✅
- ✅ Deck lanes; drag sections into place
- ✅ Live checks as you build
- ✅ Save and load plans as JSON

**Phase 3: Simple solver** ✅
- ✅ Same-track hot cue jumps, loops and beat jumps
- ✅ Bar-by-bar cheat sheet

**Phase 4: Multi-deck timing** 🟡 (current)
- ✅ Pre-rolls with the fader down
- ✅ Muted re-entry with a relaxed beat jump
- ✅ Two-hands check (set for beginners)
- ✅ Get-ready groups on the cheat sheet
- ✅ Tempo, key, vocal-clash and off-phrase checks
- ✅ Background stem analysis
- ⬜ Looping an intro or outro to stretch it for a long mix

**Phase 5: Rekordbox export** ⬜
- ⬜ Export the planned cues as Rekordbox XML

**Phase 6: Preview player** 🟡
- ✅ Preview a single section in the browser
- ⬜ Play back a whole set with its mixes

**Extras** ✅
- ✅ Own EDM phrase detection, with a Rekordbox / Auto toggle
- ✅ Manual phrase editor (Custom sections)

**Phase 7: Nice-to-haves** ⬜
- ⬜ Audio render (ffmpeg)
- ⬜ Serato and Traktor support
- ⬜ Optional database write (only with your explicit OK, Rekordbox closed, timestamped
  backup first)

## Safety

- **Your Rekordbox database is never modified.** Each time the app loads, it copies
  `master.db` into `.cache/` and reads only the copy. That also means it works with
  Rekordbox open.
- Analysis files (`share/PIONEER/USBANLZ/...`) and audio files are only ever **read**.
- Cues will go back into Rekordbox via XML import (Phase 5), not by writing to the database.
- The web server only listens on `127.0.0.1` (this computer).

## Setup (Windows)

Needs Python 3.10 or newer (built and tested on 3.14).

```powershell
git clone https://github.com/liamgibsonn/ezmix.git
cd ezmix
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Optional, for the mixing checks that need **stems** (vocal clashes, bass-swap timing):
PyTorch + Demucs, about 1-2 GB (see the commands in `requirements.txt`). Without them
everything else works.

**In Rekordbox**, for the best results:
1. Preferences → Analysis: tick **Phrase** in the track analysis settings.
2. Select your tracks in Collection → right-click → **Analyze Track**. Turning on the
   setting doesn't re-analyse tracks you've already analysed.
3. Tracks without phrase analysis still work. They're split into plain 16-bar blocks.

## Run

```powershell
cd ezmix
.\.venv\Scripts\python -m djplanner
```

Open **http://127.0.0.1:8000** in your browser. Press Ctrl+C in the terminal to stop.

The server restarts itself whenever the code changes, and the app also checks the
phrase-detection code itself each time a track is opened, re-analysing tracks if it
changed, so **refreshing the page shows the latest rules**. The
first view of each track after a change takes a few seconds while it's re-analysed. Add
`--no-reload` to turn the auto-restart off.

After changing things in Rekordbox (analysing, adding tracks, setting cues), click
**Reload library** in the app.

## Using it

### Phrases: Auto (EDM), Rekordbox or Custom
Rekordbox's phrase analysis uses song-structure names (Verse, Chorus, Bridge) that fit
EDM poorly. The app has its **own detection** made for dance music, and a toggle above
the track list to switch between them:

- **Auto (EDM)** (default): Intro · Build · Pre-drop · Fake drop · Drop · Groove ·
  Breakdown · Outro, numbered where needed (Drop 1, Drop 2…).
- **Rekordbox**: Rekordbox's own phrases, exactly as Rekordbox shows them.
- **Custom**: your own sections, made with the phrase editor (below). Blank for a track
  until you add some; blank tracks still work with **Add bars … to …** in a track's
  details. (Replaced the old "None" option.)

### Editing sections yourself (✎ Edit)
Hover a track and press **✎ Edit** (not available on Rekordbox phrases).
- On **Auto (EDM)** it copies the detected sections into your Custom version of that
  track (asking first if you already have one), switches to **Custom** and opens the
  editor, so you start from the detection and only fix what's wrong.
- On **Custom** it edits what you have (or a blank strip).

In the editor, on the track's strip:
- **drag an edge** to move it (snaps to whole bars). Where two sections touch, only one
  moves: drag left and the left one shrinks, drag right and the right one shrinks (a gap
  opens). **Shift+drag** moves both together. Drag an edge right across its section to
  **delete** it.
- **drag a section by its middle** (or **Alt+drag** anywhere on it, handy for 1-bar
  sections) to slide it sideways into an empty gap next to it
- **click** a section to select it, then:
  - change its **type** in the list
  - **Split in half**: 50/50; then drag the edges / parts where you want them
  - **+ Add after**: a 1-bar section straight after it (in the gap if there is one,
    otherwise it takes the selected section's last bar), typed Pre-drop after a Build,
    Build otherwise; change the type if needed. Handy for 1-bar builds and pre-drops.
  - **merge** it with the section before/after, or **delete** it (Delete key)
- **double-click** a section to split it at that bar
- **drag across an empty part** to add a section (start away from other sections'
  edges, or you'll grab the edge instead)
- **right-click** still plays a section, so you can check by ear as you go
- **Ctrl+Z / Ctrl+Y** undo / redo (while you're working on that strip)
- an optional **note** saves why (e.g. "bass still in at 41, so it's drop")
- **Reset to Auto (EDM)** / **Clear all** start again; neither is kept unless you Save
- **Save** / **Cancel**

Your sections are saved in `custom_phrases.json` in the project folder (never in
Rekordbox). Edited tracks also count as labels for scoring and tuning the Auto (EDM)
detection (`phrase_eval`), so editing a track is how to tell Claude what's wrong with it.

If a track's beatgrid starts a few beats before bar 1 (e.g. a tempo change means the
start doesn't line up), those beats are drawn as part of the first section, so there's
no blank gap.

How the auto detection works (`djplanner/audio_analysis.py`):
1. ffmpeg decodes the track to raw audio in memory (the file is only read).
2. For every bar on the beatgrid it measures loudness, sub/kick bass, highs, and a
   12-band "fingerprint".
3. It splits where the sound changes most (in dB), preferring 8/16/32-bar phrases,
   and also tries the phrase grid shifted by 1–3 bars in case bar 1 isn't where
   the phrasing starts.
4. It names each section, using rules worked out from hand-labelled tracks:
   - **Drop**: bass and loudness within a dB or two of the track's loudest section.
     Intros with bass, builds and breakdowns can be only 2–4 dB quieter, so it's strict.
   - **Build**: the highs rise (risers, rolls), the bass thins out over the 8 bars
     before a drop, or the whole thing swells louder into the drop. The last 2 bars of
     an intro/groove/breakdown are a short build if their highs suddenly open up right
     before a drop. Straight after a drop it breaks down first: a "build" there that's
     followed by a much brighter build is really the breakdown. The top of a build's
     climb stays part of the build even if it's nearly at full level. The last 4 bars
     before a drop are a build if they sound clearly different, the bass thins and it
     doesn't get quieter. A bass dip that gets *louder* overall isn't a pre-drop cut.
     A build is never longer than 16 bars. A "build" that's stripped back (bass out)
     and flat, and only swells in its last 2 bars, is a breakdown + a 2-bar build.
     A later stretch
     that's a bar-for-bar copy of an earlier build (+ pre-drop) gets the same labels.
   - **Pre-drop**: 1–4 bars right before a drop where the bass is pulled out, found
     bar by bar (so it doesn't have to fit the 8-bar grid).
   - **Fake drop**: the bass slams back in after a build, like a drop, but it isn't
     full level, and it goes into *another* build before the real drop. Tracks often
     copy the first drop's build-up exactly, so only what comes after tells them apart.
   - **Drop** vs **Groove**: both can be at full level. A **drop** is where the bass
     *slams back in* after a cut or build. A **groove** just carries on (after the
     intro, or after a short break) and leads into a real slam-in drop; a section that
     sounds like an earlier groove is that groove coming back. A drop never goes
     straight into a groove: what follows a drop is a breakdown. And a drop (almost)
     always has a build before it (even a 2-bar one): a full-level part straight after
     the intro with nothing building into it is a groove, not a drop. Before the first
     build, the section where the bass first comes in is still the intro; the intro is
     over (it's a groove) once the bass is in at full drop level, or when the next
     section keeps the bass at least as strong. If the bass drops back, it's still the
     intro. A later "fake drop"
     that sounds like that groove is the groove coming back, and the bit between the
     drop and it is a breakdown. Likewise, if the opening (with the bass in) comes back
     bar for bar at the end of a breakdown, those bars are a groove.
   - **Short bass cuts** (1–4 bars) right before a drop are named by what came before:
     after a build → **Pre-drop**; after a groove/intro → **Build**; after a drop →
     **Breakdown**; after a breakdown → part of that breakdown.
   - **Intro / Breakdown / Outro**: quiet sections before anything happens / between
     drops / after the last drop. The opening section is always an intro (a 1-bar
     scrap before the beat comes in doesn't count as the opening).

**Checking it against your ears:** `phrase_labels.json` holds hand-labelled tracks
(bar numbers as shown in this app). It's personal (your own tracks), so it's kept out
of git along with `custom_phrases.json`, `plans/` and `path_remaps.json`. Run

```powershell
.\.venv\Scripts\python -m djplanner.phrase_eval      # add -v for bar-by-bar differences
```

to see how close the detector gets (label accuracy, boundaries found, extra splits).
Add more tracks to that file to make the tuning more reliable.

It takes about 0.3 s per track the first time, then results are cached in
`.cache/phrases/` (redone automatically if the file, beatgrid or settings change).
**It relies on the beatgrid**: if bar 1 / the downbeat is wrong in Rekordbox, fix the
grid there first. All the tuning numbers are in `SETTINGS` at the top of
`audio_analysis.py`. If a track can't be analysed (e.g. its file is missing) it falls
back to Rekordbox's phrases with a warning.

### Library (bottom half)
- Each track shows its **phrase strip**: coloured blocks for Intro / Up / Down /
  Chorus / Outro (etc.), a waveform drawn per beat on top, bar numbers along the top,
  and existing cues as flags (yellow = hot cue with its pad letter, red "M" = memory
  cue). A white line along the bottom of a phrase = Rekordbox detected a **fill**.
- Hover the strip to see the exact bar/beat, and the section's name and length.
- **Right-click a section** to listen to it (right-click it again to pause).
- **Hold right-click** (or hold and drag) to jump to an exact spot: playback follows
  the pointer while you hold, then carries on to the end of the track from where you
  let go.
- **Hover a track** to show its controls in its title row (they stay while it's playing
  or paused): **▶ / ❚❚** (▶ on a track that isn't playing starts it from the top and
  stops whatever else was playing), **−8 / +8** bars, and the current bar and beat.
  **Pause is how you stop**: the controls stay on that track (dimmed, at the bar you
  stopped on) until you play another track, and ▶ carries on from there. While a
  **section** is previewing ("bars x–y") there's also a **✕**: it drops the section limit
  and carries on playing through the rest of the song. Space plays/pauses whatever is loaded.
- **MP3s are previewed from a WAV copy** (made once with ffmpeg, kept in `.cache/audio/`,
  about 10 MB per minute; safe to delete). Browsers jump to the wrong spot in MP3s,
  especially variable-bitrate ones like Audacity exports, which made tracks sound a beat
  off here while they were fine in Rekordbox. Your MP3 is only read, never changed.
- Click a track's title row for its phrase and cue tables, warnings, and an
  **"Add bars X to Y"** form for custom ranges.

### Building a set (timeline, pinned at the top)
The timeline has **lanes**. Each block is a stretch of a track, placed on a lane at
a position in the set:

```
Set bar   1       9       17      25      33
Lane 1  [Intro  ][Chorus 1        ][Outro  ]
Lane 2                    [Intro          ][Up]
Mixes                     [ 8-bar mix  ]
```

Lanes aren't decks: the solver picks which deck each lane is played on (see the
cheat sheet's "Tracks on each deck"). Lanes that never play at the same time can take
turns on one deck, as long as the earlier track finishes at least 2 bars
(`LOAD_GAP_BARS`) before the next one starts, to load it (`djplanner/lanes.py`). So
there's **always one empty lane** at the bottom: put a 5th song there on a 4-deck
setup and it replaces a track that has finished. If more lanes need a deck at once
than you have decks, the blocks involved get an error.

- **Click** a phrase on any strip to add it to the end of the **active lane**. Click a
  lane's name to make it active.
- **Drag** a phrase onto any lane to place it exactly. A dashed preview shows where it
  will land, and turns red if it would collide with another block on that lane.
- Drag blocks to move them in time or between lanes. They snap to the grid (set in
  **Snap**) and to the edges of other blocks.
- Where blocks on different decks overlap, that's a **mix**. It's shown in the Mixes row.
  You decide *what* plays and roughly where the mixes are. The solver (Phase 3/4)
  works out *how*: hot cues, loops to stretch a section, beat jumps, fades.
- Click a block to edit it: name, deck, set bar, which track bars it plays and its
  length (changing the track range makes it a custom block), duplicate or remove.
- **Double-click a block** (or select it and press **Space**, or use ▶ Preview) to
  hear it. Space also stops whatever is playing.
- Your **deck setup** is saved with the set:
  - **Tracks** (1–6): how many tracks you can have playing at once, i.e. how many decks
    you have, e.g. 3 for three CDJs.
  - **Jog wheels** (1–6): how many sets of deck controls you have
    (jog wheel, pads, PLAY/CUE, loop and beat-jump buttons). With fewer jog wheels than
    tracks, decks share one and you switch with the deck-select button, like the
    DDJ-FLX6-GT (4 tracks, 2 jog wheels). Decks are dealt out in turn: with 2 jog wheels,
    Decks 1, 3, 5 are on the left and 2, 4, 6 on the right; with 3, it's 1+4, 2+5, 3+6.
    Channel faders and EQs are
    separate for every deck, so they never need switching.
  - **Instant doubles**: the same track on two decks at once.

As you edit, the set is checked (in Python) and problems show up straight away:

| Border | Meaning | Examples |
|---|---|---|
| Red, solid ⛔ | Can't be played like this | two blocks on one deck overlap; a deck switches track with no time to load it; same track on two decks without instant doubles; block runs past the end of its track; deck not in your setup |
| Yellow, dashed ⚠ | Works, but probably not what you want | audio file missing; silence (listed under the timeline) |
| (icon only) ℹ | Worth knowing | a hard cut between tracks with no overlap |

| Key | Does |
|---|---|
| Ctrl+S | Save |
| Ctrl+Z / Ctrl+Y | Undo / redo |
| ← / → | Select previous / next block |
| Alt+← / Alt+→ | Nudge the selected block by one snap step |
| Alt+↑ / Alt+↓ | Move the selected block to the deck above / below |
| Space | Play / pause the preview, or listen to the selected block |
| Ctrl+D | Duplicate (copy goes straight after it on the same deck) |
| Delete | Remove the selected block |
| Esc | Deselect |

### How it gets played: the solver (Phase 3)
As you edit, the solver works out the physical moves (they're listed on the cheat
sheet). Any problems with them, like a step your hands can't reach in time, show on the
block they're about, like the other warnings. (The timeline used to show each step as
a little marker; removed 2026-10-15, the user found them more confusing than useful.)

With shared jog wheels, the cheat sheet adds a step like **"Left side → Deck 3: press
its deck-select button (any time between set bar 9 and 17)"** whenever a side has to
change deck before a pad, beat jump, loop or PLAY. Each side starts on its lowest deck
(Deck 1 left, Deck 2 right). Loading and stopping (fader down) don't need a switch. If a
side has to do something on one deck and then on the other less than 2 bars later
(`SWITCH_WINDOW_BARS` in `solver_config.py`), it's flagged, with a suggestion to move one
block to a deck on the other side.

What it handles so far (within one track on one deck):

| You put… | It uses |
|---|---|
| The next section straight after | nothing |
| The end of the previous section again (once or several times) | a loop (auto beat loop for 1/2/4/8… bars, otherwise LOOP IN/OUT), or a beat jump back |
| A jump by a power-of-two number of beats | a beat jump (up to 2 presses, e.g. +32 then +16) |
| Any other jump | a hot cue pad: **your own cue if one is already on that bar**, otherwise a new one on a **free** pad (yours are never touched) |
| No free pads and no beat jump fits | "render as an audio edit" |

Each run starts from a memory cue (press PLAY) or a pad, and stops at its end.

### Getting every track in on time (Phase 4)
YOU decide what plays and how you mix it (fade, cut, echo, backspin, bass swap...).
The planner's job is the mechanics: every track in the right place at the right
moment, in a way one person (a beginner, even) can actually do.

- **Pre-roll** (setting next to Decks / Jog wheels, default 4 bars): each track is
  started that many bars before it comes in, **with its fader down**, so bringing it in
  is just **fader up** (it's already playing in time). If the deck is busy or the track
  starts too soon, it uses a shorter pre-roll, and only as a last resort a PLAY / pad
  press right on the bar.
- **Bringing a track back** on the deck it was on (e.g. back to its "BREAK DOWN" cue
  after another track): instead of stopping it, **fader down but leave it playing**, then
  a **beat jump any time in the gap** puts it exactly where it needs to be when you
  fader it back up. One relaxed button press, no timing pressure.
- **Relaxed steps** (things you can do any time in a stretch, e.g. while a track is
  muted) are shown on the cheat sheet as "**51.3 before**": do it any time before then.
- **Your two hands on the controller** (set for beginners): every step is mapped to the
  real control on a **DDJ-FLX6** layout (`djplanner/controllers.py`: channel faders 3·1·2·4,
  PLAY/CUE, the 8 pads and their mode buttons, loop IN/OUT/RELOOP, DECK buttons), and
  both hands are walked through the set (`djplanner/hands.py`). Moving to a control
  within 8 cm (along the fader row, around the pads) costs nothing; further takes
  0.25 s plus the distance at 60 cm/s (left PLAY → right pads ≈ 1.2 s). If no hand
  can get there in time it's flagged **Too tight**, saying which move; if it only just
  makes it, a **Busy moment** note.
- **Pad modes**: hot cues need HOT CUE mode, beat jumps BEAT JUMP mode, auto loops BEAT
  LOOP (SHIFT + BEAT JUMP). When a step needs a different mode, it says so: "Pad mode →
  BEAT JUMP, then …".
- **"Any time" steps** (muted beat jumps, deck switches) are placed where a hand is free
  and already close, so one side's prep gets done together.
- **Cheat sheet**, written to be read at a glance: the **bar.beat** first, then the
  deck, then a **short instruction** ("PLAY (fader DOWN)", "Fader UP", "Beat jump back
  16 bars"), with the details small underneath ("4 bars early · comes in at 53.1").
  Cueing ("Go to the memory cue at track bar 13") and pad-mode changes ("Pads → BEAT
  JUMP mode") are their own steps. Every step is tinted in its deck's colour. Hover a step for the full sentence.
- **Mix checks** (on the incoming block, information only): **tempo gap** over 6% (half /
  double time counts as the same), **key clash** (not neighbours on the Camelot wheel),
  **vocal clash** (both tracks have vocals during the overlap), and a note when a
  track comes in **part-way through a phrase** of the other.

**Stems** (drums / bass / other / vocals, bar by bar) for the vocal check come from
Demucs, run in the background about a minute per track the first time a track is in a
mix, and saved in `.cache/stems/` (your audio is only read). The page re-checks every
20 seconds while any are still being worked on.

### Cheat sheet
Click **Cheat sheet** (it saves the set first). You get a page made to read on a
phone or tablet:
- **Tracks on each deck** at the top: which track(s) go on which deck, in order (and
  which side of the controller). When to load them is up to you, so loading isn't a step.
  The lanes aren't fixed to Decks 1-4: the solver tries every lane-to-deck assignment
  and picks the easiest (fewer presses, deck switches, tight or busy moments; weights in
  `DECK_CHOICE_WEIGHTS`, solver.py). It only moves a lane for a real gain, not a bit
  less hand travel. A DOUBLE (two tracks playing at once) never goes on one shared
  jog wheel, so you can always reach either track without switching decks mid-mix;
  the planner takes extra deck switches to avoid it, and warns ("Double on one jog
  wheel") if there's no way round it (e.g. 3 tracks at once on 2 jog wheels). The timeline's lanes just say "Lane 1", "Lane 2"... (no deck numbers, so nothing
  reads "Deck 3" when it's really played on Deck 4); the cheat sheet lists the tracks
  by their real deck.
- **Steps**: every action in order, with the set bar and beat in big text
  (`17.4` = bar 17, beat 4; everything is in bars, no clock times) and the deck.
  Getting ready before anything starts (loading and cueing the first track) has no
  bar number; other steps you can do at any point before a bar say "before" under it.
  Tap a step to tick it off.
- No "prep before the set" list: it's for one transition at a time, done on the fly.
  A step that needs a cue you don't have says how to make it there and then ("Find
  track bar 13, press CUE"; "first set pad C at track bar 33").
- **Download .txt** for a plain-text copy, or **Print**.

To open it on your phone, the planner has to be reachable from it: set `HOST` to
`"0.0.0.0"` in `config.py` (or set `DJPLANNER_HOST=0.0.0.0`) and browse to your PC's
IP address, port 8000, from the phone on the same Wi-Fi. Only do that on a network you
trust: the app then serves your track list to anyone on it. The text download is the
simpler option.

### Saving
- Name the set at the top, then **Save**. **Open…** lists your saved sets, and **New**
  starts a blank one.
- Sets are saved as JSON in `plans\<id>.json`. **Delete** moves the file to
  `plans\.trash\` instead of erasing it.
- Unsaved work is also kept in your browser, so refreshing the page doesn't lose it.
- Sets saved before deck lanes existed open with all their blocks back to back on Deck 1.
- Sets saved before the jog wheel setting existed open with one jog wheel per deck.

## Settings

Everything editable is in [`djplanner/config.py`](djplanner/config.py):

| Setting | What it does |
|---|---|
| `REKORDBOX_DIR` | Where `master.db` lives (default `%APPDATA%\Pioneer\rekordbox`) |
| `PATH_REMAPS` | Old folder → new folder, for tracks Rekordbox still has at an old location. Set in `path_remaps.json` in the project folder, e.g. `{"D:/Old/Music": "C:/Users/you/Music"}` (kept out of git, it holds your own folders) |
| `FALLBACK_BLOCK_BARS` | Block size for tracks without phrase analysis (default 16) |
| `HOST` / `PORT` | Web server address |

## Tests

```powershell
.\.venv\Scripts\python -m pytest
```

The tests use small fake tracks and don't need Rekordbox.

## Project layout

```
NEXT_TIME.md           where the last session left off + your to-do list (local only)
djplanner/
  config.py            user settings (paths, remaps, block size)
  models.py            data model: Track, Phrase, Cue, Segment, Plan
  beatgrid.py          bars <-> seconds conversion (pure logic)
  analysis.py          raw Rekordbox analysis -> phrases / cues / waveform (pure logic)
  keys.py              key -> Camelot wheel
  rekordbox_reader.py  snapshot + read the Rekordbox library (read-only)
  plans.py             save/load set plans (plans/*.json) + the live layout checks
  lanes.py             lanes vs decks: which lanes can share a deck, decks needed at once
  solver.py            works out how to perform a plan (shortest-path search)
  solver_config.py     the solver's scoring weights: edit these to change its choices
  cheatsheet.py        turns a solved plan into bar-by-bar steps (JSON / text)
  audio_analysis.py    our own EDM phrase detection (Intro/Build/Pre-drop/Fake drop/Drop/...)
  phrase_eval.py       scores the detection against phrase_labels.json (+ your Custom edits)
  custom_phrases.py    your own sections from the phrase editor (custom_phrases.json)
  preview_audio.py     MP3 -> cached WAV copy for exact in-browser seeking
  mixing.py            Phase 4: finds mixes between decks + tempo/key/vocal/phrase checks
  stems.py             background stem analysis (Demucs), cached in .cache/stems/
  controllers.py       where every control is on the controller (DDJ-FLX6, in cm)
  hands.py             your two hands moving over the controller: too tight / busy
  user_settings.py     settings changed from inside the app (user_settings.json)
  api.py               FastAPI web API
  __main__.py          `python -m djplanner` starts the server
web/
  index.html, style.css
  app.js               wires the page together (library, timeline, plan controls)
  timeline.js          the set timeline: deck lanes, drag & drop, snapping, editing, undo
  phrase-strip.js      draws a phrase strip on a canvas
  phrase-editor.js     the phrase editor (✎ Edit): editing logic + its toolbar
  player.js            section preview (plays a bar range of a track)
  cheatsheet.html/.js/.css  the cheat sheet page
  api.js               fetch helpers for the backend
plans/                 your saved sets (created on first save; local only)
tests/                 pytest tests with fake tracks
  js/                  JavaScript tests (Node), run by pytest via test_web_js.py
```

### How positions work

Everything is measured in **bars**, never seconds. Bar 1 starts on the track's first
downbeat, and each beat is 0.25 of a bar, so `32.75` = bar 32, beat 4. Seconds are only
used at the edges (reading cue times, playing audio), converted through the track's
beatgrid (`beatgrid.py`).

### Plan file format

```json
{
  "version": 2, "id": "4faabee5a629", "name": "Friday warm-up",
  "settings": { "decks": 4, "jog_wheels": 2, "instant_doubles": false },
  "segments": [
    { "id": "…", "track_id": "123", "deck": 1, "set_bar": 33,
      "start_bar": 17, "length_bars": 16,
      "label": "Chorus 1", "kind": "chorus", "source": "phrase",
      "track_title": "…", "track_artist": "…", "bpm": 174 }
  ],
  "actions": []
}
```
`set_bar` is where the block starts in the **set**; `start_bar` / `length_bars` are
which part of the **track** it plays. All positions are in bars, rounded to whole beats.
`actions` holds the solver's output, saved with the set.

### Why plain JavaScript (no React/Vue/build step)

FastAPI serves the files in `web/` directly, so there's no Node/npm toolchain to
install or build. Every file is readable as-is, and you can edit it and refresh the
browser. The phrase strips are drawn on `<canvas>` and the timeline is plain HTML with
the browser's built-in drag and drop, so no framework is needed. If the UI grows a lot, Preact or Svelte can be added later.

## Known limits / notes

- **Hot cue pad mapping**: Rekordbox stores the pad as a number. Pads A (1) and B (2)
  are confirmed. C–H are assumed to be 3–8 but **unverified**. To check, set hot
  cues on pads C–H on one track, click Reload, and open that track's details. If
  the letters are wrong, fix `HOT_CUE_KIND_TO_PAD` in `analysis.py`.
- With **Rekordbox** phrases, a long drop is often split into several consecutive
  "Chorus" phrases (Chorus 1, Chorus 2, …). Use right-click preview to tell them apart,
  or switch to **Auto (EDM)** phrases.
- Auto phrase detection is heuristic: it's usually right about where drops and
  breakdowns are, but odd structures (fake drops, very long intros with bass) can
  fool it. A manual phrase editor is planned next.
- Loading parses every track's analysis (~0.1 s per track). That's fine for hundreds of
  tracks but slow for many thousands. It can be optimised later if needed.
- pyrekordbox quirk: never test an `AnlzFile` with `if anlz:`, because it recurses forever
  on Python 3.14. Use `is not None`.

