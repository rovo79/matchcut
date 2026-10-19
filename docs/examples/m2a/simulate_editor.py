#!/usr/bin/env python3
"""Simulate an editor's changes to an exported matchcut OTIO file, the way a script would (no editor involved).

    python docs/examples/m2a/simulate_editor.py EXPORTED.otio EDITED.otio [--strip-metadata]

Three edits, applied to the first video track and to every audio track that mirrors it (same clip names; a music track is left alone):
  1. the second clip is trimmed by 5 frames at its head,
  2. the first dissolve is turned into a cut (the Transition item is deleted),
  3. the last clip is deleted (with its junction marker).
--strip-metadata also removes every matchcut metadata block and renames the clips, as an application that does not keep third-party
metadata would: the importer then has to find the clips by their source ranges.

Standard library only.  This simulates what an editor does; it does not prove what any editor does (that is the human gate).
"""
from __future__ import annotations

import json
import sys


def is_clip(item: dict) -> bool:
    return item["OTIO_SCHEMA"].startswith("Clip")


def is_transition(item: dict) -> bool:
    return item["OTIO_SCHEMA"].startswith("Transition")


def edit_track(track: dict, strip: bool) -> list:
    """Apply the three edits to one track; return what was done."""
    kids = track["children"]
    clips = [k for k in kids if is_clip(k)]
    done = []
    trim = clips[1]  # 1. trim the head of the second clip by 5 frames
    trim["source_range"]["start_time"]["value"] += 5
    trim["source_range"]["duration"]["value"] -= 5
    done.append(f"trimmed the head of {trim['name']} by 5 frames")
    firsts = [i for i, k in enumerate(kids) if is_transition(k)]  # 2. dissolve -> cut
    if firsts:
        first = firsts[0]
        done.append(f"deleted the transition before {kids[first + 1]['name']} ({kids[first]['name']})")
        del kids[first]
    else:
        done.append("no transition to turn into a cut")
    gone = [k for k in kids if is_clip(k)][-1]  # 3. delete the last clip
    done.append(f"deleted the clip {gone['name']}")
    kids.remove(gone)
    if track.get("markers"):  # the junction into the deleted clip goes with it
        track["markers"].pop()
        done.append("deleted the last junction marker")
    if strip:
        for n, k in enumerate(k for k in kids if is_clip(k)):
            k["metadata"] = {}
            k["name"] = f"edit {n + 1}"
        for m in track.get("markers", []):
            m["metadata"] = {}
    return done


def main(argv: list) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8") as fh:
        doc = json.load(fh)
    tracks = doc["tracks"]["children"]
    video = next(t for t in tracks if t.get("kind") == "Video")
    names = [k["name"] for k in video["children"] if is_clip(k)]
    for track in tracks:
        mirror = track is video or (track.get("kind") == "Audio" and [k["name"] for k in track["children"] if is_clip(k)] == names)
        if not mirror or len(names) < 2:
            print(f"{track['name']}: left alone")
            continue
        for line in edit_track(track, "--strip-metadata" in argv):
            print(f"{track['name']}: {line}")
    if "--strip-metadata" in argv:
        doc["metadata"] = {}
    with open(args[1], "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"wrote {args[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
