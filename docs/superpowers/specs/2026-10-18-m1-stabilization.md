# M1 stabilization — design

Status: proposed. Amends `docs/superpowers/specs/2026-10-17-m1-junction-inspector.md`
(binding where this document is silent).

M1's human acceptance gate (`docs/M1-ACCEPTANCE.md`) stays open: it needs a real
model backend and an image-capable terminal, neither of which a development
container has. This work fixes what the mock-backend rehearsal of that gate
found, closes the two edge cases the M1 final review deferred, and adds
deterministic regression tests that exercise cut points that really move,
clips that really reorder, dissolves that must survive and pins that must
persist. It does not add UI.

## 1. Wording fixes

### 1.1 A reverted pin is not "locked"

The rehearsal showed a reverted junction labelled three ways at once:
`locked by the editor · … · pinned (reverted)`, a history line
`editor (before matchcut): locked`, and a why line `reverted by the editor`.

- `Junction.pin_origin` (new property): the `origin` recorded in the data of the
  latest `editor`/`pinned` decision attributed to the junction (`"locked"` or
  `"reverted"`), `None` when there is none.
- Inspector badge for a locked junction that is pinned: `locked by the editor`
  when `pin_origin` is `"locked"`, `reverted by the editor` when it is
  `"reverted"`. A match-cut lock still reads `locked by matchcut`.
- History: the stage a pin was applied before reads
  `editor (before <stage>): <origin>` instead of `…: locked`. Any other change
  in that line (points moved by the clamp, a transition) is kept.
- `junction_line` (the CLI) keeps its `locked` and `pinned` tags; it adds no
  origin tag.

### 1.2 No negative zero

Any signed number the inspector or the CLI prints (cut-point deltas in
seconds, the score Δ column) prints `+0.00`, never `-0.00`, when it rounds to
zero. One helper, `junctions.signed(value, places=2)`, does the formatting and
every signed print goes through it.

## 2. A pin on a junction that a pass created is placed

M1 left a known gap: a junction that only exists after a pass (pacing dropped
the clip between two shots, continuity inserted a separator, the match-cut
pass reordered a beat) could be pinned, but the next run reported
`pin not placed: … not adjacent in this cut`, because pins are applied to the
rough cut, before any pass. A pin says "keep shot A immediately followed by
shot B at these points", so `apply_pins` now makes the two shots adjacent
when they are not:

1. Choose the two clips the pin points at. A pin remembers the clip instances it
   was taken on: `Clip.origin` (default `""`, serialised) is the identity of a
   clip instance, `timeline.clip_origin(c)` returns `c.origin` when set, else
   `"{shot}@{in:.3f}-{out:.3f}"` (the rough-cut key). `Pipeline.refine` stamps
   every rough-cut clip that has no origin with its `clip_origin` before
   `apply_pins`; every pass copies clips (continuity's merge keeps the earlier
   piece), so the origin reaches every stage's timeline. `Pin` gains
   `out_origin` and `in_origin` (default `""`, serialised; `Pin.key` and
   `pin_outcome` are unchanged), which the Edit view fills with `clip_origin` of
   the junction's two clips in the inspected stage's timeline (empty when a clip
   cannot be found). Per side (outgoing: the out shot, `out_time`, `out_origin`;
   incoming: the in shot, `in_time`, `in_origin`) over the candidates, the clips
   of that shot, using the ranges the clips had **before the first pin was
   applied** (a snapshot taken at the start of `apply_pins`, so a clamp by an
   earlier pin never changes a later pin's choice):
   - lineage: if the pin's origin is set and exactly one candidate has that
     `clip_origin`, it is chosen;
   - otherwise the nearest boundary: the outgoing side measures
     `abs(out_time - source_out)`, the incoming side `abs(in_time - source_in)`.
     One candidate is chosen. With several, the nearest is chosen only if the
     runner-up is at least `AMBIGUITY_SECONDS = 0.5` farther; otherwise the pin
     is **not placed** and nothing moves: `pin_unplaced`, detail
     `shot N is ambiguous: more than one piece fits the pinned point`. A pin is
     never placed on a guess.
   The two clips are never the same clip: for a pin on one shot (a jump cut) the
   incoming side excludes the clip the outgoing side chose. If a shot is not in
   the cut: `pin_unplaced`, detail `shot N is not in this cut`. If a jump-cut
   pin's shot has only one clip in the cut: `pin_unplaced`, detail
   `shot N has only one clip in this cut`.
2. If the chosen incoming clip directly follows the chosen outgoing clip, place
   the pin as today. A pair of the same two shots that is adjacent elsewhere in
   the cut is used only when the points select it.
3. Room check, before any move: if the pinned points do not fit the two clips
   (the shot bounds and the minimum clip length), `pin_unplaced`, detail
   `not enough room in these clips for the pinned points`. The clip order is
   left untouched and no `pin_moved` is recorded.
4. Otherwise try, in order: move the incoming clip to just after the outgoing
   clip; move the outgoing clip to just before the incoming clip. A candidate is
   rejected when:
   - the moved clip already has a locked end (another pin holds it):
     `shot N is held by another pin`;
   - the anchor's side is locked (the outgoing clip's out-point, or the
     incoming clip's in-point, already belongs to another pin):
     `shot N is held by another pin`;
   - the new order would split a beat or change the order of the beats (the
     sequence of beat-id runs in clip order must stay exactly as it was):
     `moving shot N would split or reorder the beats`.
5. Apply the first candidate that passes. Record
   `Decision("editor", "pin_moved", [moved clip id, anchor clip id], "moved shot X next to shot Y to hold the editor's pin", None, pin.to_dict())`
   and then place the pin as in step 2 (its usual `pinned` decision follows).
   The moved clip keeps its beat id.
6. If both candidates are rejected: `pin_unplaced` with the first candidate's
   reason.

The reasons a pin is unplaced are therefore exactly six: `shot N is not in this
cut`, `shot N has only one clip in this cut`, `shot N is ambiguous: more than one
piece fits the pinned point`, `shot N is held by another pin`, `moving shot N
would split or reorder the beats`, and `not enough room in these clips for the
pinned points`.

Placement is two-phase, each phase in store order: first every pin whose chosen
clips are already adjacent, then the pins that need a move. A clip locked by a
pin placed earlier in the same call is never moved, so an adjacent pin is
protected whatever its place in the store. The junction data layer treats
`pin_moved` like a `drop` or `insert` when it explains why two clips became
adjacent (`made adjacent by …`). The `editor` tier of `why()` already ranks the
`pinned` decision first. Junctions a pass created are attributed their pass
decisions by the lineage of their clips (a continuity insert names all three
clips it touched; a match-cut reorder's verdicts about the new neighbours): this
is intended.

The Edit view's notification when the editor pins such a junction changes from
"…the pin only applies if a regenerated rough cut pairs them" to:
`these shots are not adjacent in the rough cut: the next run moves one of these shots next to the other, unless that would split or reorder the beats or another pin holds them`.

Consequence, documented: the match-cut pass does not reorder a beat that holds
a locked clip, so a pinned beat is no longer reordered.

## 3. A pinned jump cut is still softened

`ContinuityPass` skipped any junction whose incoming clip had a locked in-point,
so a pin, which locks both ends, also stopped continuity from softening a jump
cut with a dissolve. A pin holds the cut points, not the transition. Rule:

- a match cut (`passes.is_match_cut(a, b)`): skip, as today;
- otherwise, if either end of the junction is locked (`a.locked_out` or
  `b.locked_in`): never insert a separator between the two clips (it would
  break the pin), but still soften the jump cut with the usual dissolve when
  the junction is a hard cut;
- otherwise: unchanged (separator first, dissolve when there is none).

## 4. Regression coverage

New deterministic tests on the synthetic fixture footage and hand-built
timelines, with the mock backend. Required scenarios:

1. **Cut points that really move:** a mock run with a tight target duration
   where pacing trims clips; at least one junction's out- or in-point moves by
   at least one frame; its history names the pass; no printed delta is `-0.00`.
2. **Reordered clips:** a hand-built rough cut where a pinned junction's shots
   are not adjacent; after `refine` the pin is placed by a move (a `pin_moved`
   then a `pinned` decision), the junction is adjacent from the first pass
   stage on and its why line is the editor's; a move that would split a beat
   reports
   `pin not placed: moving shot N would split or reorder the beats`.
3. **Preserved dissolves:** a pinned junction on a beat boundary keeps the
   transitions pass's dissolve in the final cut; a pinned jump cut gets the
   continuity dissolve; a match cut on a beat boundary keeps its hard cut.
4. **Persistent pins:** two pins (one locked, one reverted) survive a second
   run and a regenerated rough cut (re-run with the rough cut forced), and in
   both runs the badges read `pinned (locked)` / `pinned (reverted)` with the
   labels of §1.1; a rough cut regenerated without one of the shots reports
   `pin not placed: shot N is not in this cut`.
5. **Pin-to-clip selection:** repeated shots, overlapping source ranges, a pacing
   extension that lands inside a sibling piece's range, and clips reordered by a
   pass: with the clip origins the pin remembers it is placed on the right pieces;
   without them, a pin whose pieces cannot be told apart is left unplaced with
   the ambiguity reason and the clip order untouched.

## 5. Acceptance status

`docs/M1-ACCEPTANCE.md` gains a status section: automated and mock-backend
checks pass (with the date and what the rehearsal covered); the real-backend
and terminal-graphics checks are **pending a human run**. Nothing in this work
claims those as done.
