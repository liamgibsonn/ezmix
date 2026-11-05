"""
Measures how close our EDM phrase detection gets to YOUR hand-made labels.

    python -m djplanner.phrase_eval            # score every labelled track
    python -m djplanner.phrase_eval -v         # ...and show bar-by-bar differences

Labels live in phrase_labels.json (project folder), plus any track you've edited in
the app's phrase editor (Custom, custom_phrases.json): those count as labels too, and
win over phrase_labels.json for the same track (they're newer). For each it reports:
  * label accuracy  - % of labelled bars where our label (Intro/Build/Drop...) matches
  * boundaries      - how many of your section starts we found exactly / within 1 bar
  * extra           - boundaries we added that you don't have (too many splits)
With --rekordbox it also checks EVERY analysed track against Rekordbox's phrase
BOUNDARIES (not its names, which don't fit EDM). Rekordbox's phrases are often wrong,
so treat that number as a rough hint only: how many of our section starts land on a Rekordbox
boundary (+-1 bar). Rekordbox splits drops into many 8-bar pieces, so we don't expect
to find all of ITS boundaries - but ours should mostly be among them.
Use it after changing SETTINGS in audio_analysis.py to check things got better.
Read-only: it only reads the library, audio and the labels file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import audio_analysis, config, custom_phrases
from .rekordbox_reader import RekordboxLibrary

LABELS_FILE = config.PROJECT_DIR / "phrase_labels.json"

# Your label names -> our phrase kinds
KIND_OF = {
    "intro": "intro", "build": "build", "buildup": "build", "pre-drop": "predrop",
    "predrop": "predrop", "fake drop": "fakedrop", "fakedrop": "fakedrop", "drop": "drop",
    "breakdown": "breakdown", "outro": "outro", "groove": "groove", "filler": "groove",
}

# A section with no end bar ("Drop 35-…") is scored for this many bars.
OPEN_END_BARS = 16


def truth_bars(sections: list) -> dict[int, str]:
    """{bar: kind} for every bar you labelled."""
    out = {}
    for name, start, end in sections:
        kind = KIND_OF.get(name.strip().lower(), name.strip().lower())
        last = end if end is not None else start + OPEN_END_BARS - 1
        for b in range(start, last + 1):
            out[b] = kind
    return out


def detected_bars(phrases) -> dict[int, str]:
    out = {}
    for p in phrases:
        for b in range(int(p.start_bar), int(p.start_bar + p.length_bars)):
            out[b] = p.kind
    return out


def score_track(sections: list, phrases) -> dict:
    truth = truth_bars(sections)
    det = detected_bars(phrases)
    labelled = sorted(truth)
    correct = sum(1 for b in labelled if det.get(b) == truth[b])

    first, last = labelled[0], labelled[-1]
    true_starts = [s for _, s, _ in sections][1:]  # boundaries = starts after the first
    det_starts = [int(p.start_bar) for p in phrases if first < p.start_bar <= last]
    exact = sum(1 for s in true_starts if s in det_starts)
    near = sum(1 for s in true_starts if any(abs(s - d) <= 1 for d in det_starts))
    extra = sum(1 for d in det_starts if all(abs(s - d) > 1 for s in true_starts))
    return {
        "accuracy": correct / len(labelled),
        "bars": len(labelled),
        "boundaries": len(true_starts),
        "exact": exact,
        "near": near,
        "extra": extra,
        "truth": truth,
        "detected": det,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true", help="show bar-by-bar differences")
    ap.add_argument("--rekordbox", action="store_true",
                    help="also compare our boundaries with Rekordbox's (often wrong, so off by default)")
    args = ap.parse_args(argv)

    # phrase_labels.json is personal (your own tracks) and not in git, so it may be missing
    labels = (
        json.loads(LABELS_FILE.read_text(encoding="utf-8"))["tracks"]
        if LABELS_FILE.exists() else {}
    )
    # Tracks you've edited in the app (Custom) count as labels too, and replace any
    # older labels for the same track. Matched by title like the rest.
    by_lower = {t.strip().lower(): t for t in labels}
    for entry in custom_phrases.load_all().values():
        if not entry.get("sections"):
            continue
        title = by_lower.get(entry["title"].strip().lower(), entry["title"])
        labels[title] = {"sections": entry["sections"], "notes": entry.get("note", ""), "custom": True}
    lib = RekordboxLibrary()
    lib.load()
    by_title = {s.title.strip().lower(): s for s in lib.list_tracks(include_unanalysed=True)}

    totals = {"bars": 0, "correct": 0.0, "boundaries": 0, "exact": 0, "near": 0, "extra": 0}
    for title, info in labels.items():
        summary = by_title.get(title.strip().lower())
        if summary is None:
            print(f"?? {title}: not in the library, skipped")
            continue
        track = lib._base_track(summary.id)
        phrases = audio_analysis.detect_phrases(Path(summary.file_path), track.beatgrid)
        r = score_track(info["sections"], phrases)
        totals["bars"] += r["bars"]
        totals["correct"] += r["accuracy"] * r["bars"]
        for k in ("boundaries", "exact", "near", "extra"):
            totals[k] += r[k]

        print(f"\n{title}" + ("  (your Custom edit)" if info.get("custom") else ""))
        print(f"  labels {r['accuracy']:6.1%} of {r['bars']} bars | boundaries: {r['exact']}/{r['boundaries']} "
              f"exact, {r['near']}/{r['boundaries']} within 1 bar | extra: {r['extra']}")
        print("  you: " + " | ".join(f"{n} {s}-{e if e is not None else '…'}" for n, s, e in info["sections"]))
        last = max(r["truth"])
        print("  us:  " + " | ".join(f"{p.label} {p.start_bar:g}-{p.start_bar + p.length_bars - 1:g}"
                                    for p in phrases if p.start_bar <= last))
        if args.verbose:
            wrong = [b for b in sorted(r["truth"]) if r["detected"].get(b) != r["truth"][b]]
            for b in wrong:
                print(f"    bar {b:3}: you {r['truth'][b]:<9} us {r['detected'].get(b)}")

    # --- optional: every track's boundaries vs Rekordbox's -------------------------
    hit = ours = 0
    worst = []
    for summary in (lib.list_tracks() if args.rekordbox else []):
        if not summary.has_phrases:
            continue
        track = lib._base_track(summary.id)
        rb = {int(p.start_bar) for p in track.phrases}
        phrases = audio_analysis.detect_phrases(Path(summary.file_path), track.beatgrid)
        starts = [int(p.start_bar) for p in phrases][1:]
        ok = sum(1 for b in starts if any(abs(b - r) <= 1 for r in rb))
        hit += ok
        ours += len(starts)
        worst.append((ok / len(starts) if starts else 1.0, summary.title, ok, len(starts)))
    if ours:
        print(f"\nREKORDBOX BOUNDARIES (all {len(worst)} tracks): {hit}/{ours} = {hit / ours:.0%} of our "
              f"section starts land on one of Rekordbox's (+-1 bar)")
        for frac, title, ok, n in sorted(worst)[:5]:
            print(f"  lowest: {frac:4.0%} ({ok}/{n})  {title}")

    if totals["bars"]:
        print(f"\nOVERALL: labels {totals['correct'] / totals['bars']:.1%} | boundaries "
              f"{totals['exact']}/{totals['boundaries']} exact, {totals['near']}/{totals['boundaries']} "
              f"within 1 bar | extra {totals['extra']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
