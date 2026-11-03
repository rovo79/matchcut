# Editing API proof: recorded run

{'Passed': 90, 'Known gap (not a pass)': 1, 'Failed: matchcut core defect': 0, 'Failed': 0}

| Step | Check | Outcome |
|---|---|---|
| 0 fixture | the pin is applied: c001 out and c002 in are locked | Passed |
| 1 start | a session starts at r000 from final | Passed |
| 1 start | starting again resumes it | Passed |
| 2 inspect | 5 clips, 4 junctions, junction 1 shown pinned | Passed |
| 2 inspect | unused shots are listed with their descriptions | Passed |
| 2 inspect | music beat offsets per junction | Passed |
| 2 inspect | story beats with target and actual length | Passed |
| 2b refuse | a pinned edge is not trimmed | Passed |
| 2b refuse | a pinned junction is not rolled | Passed |
| 2b refuse | a pinned junction keeps its transition | Passed |
| 2b refuse | a pinned junction is not moved onto a beat | Passed |
| 2b refuse | a roll past the end of the shot is refused | Passed |
| 2b refuse | a trim below the minimum length is refused | Passed |
| 2b refuse | removing a pinned clip is refused | Passed |
| 2b refuse | moving a pinned clip away is refused | Passed |
| 2b refuse | a batch with one refused op is refused whole | Passed |
| 2b refuse | an unknown clip is named | Passed |
| 2b refuse | an unknown op is named | Passed |
| 2b refuse | no revision was made by any refusal | Passed |
| 2b refuse | a refused proposal cannot be applied | Passed |
| 1 start | r000 is on whole frames: every source point and every transition length | Passed |
| 2b refuse | a trim that leaves a transition longer than 40% of its clip is refused | Passed |
| 2b refuse | a fractional frame count is refused, not truncated | Passed |
| 2b refuse | changing one transition moves no other junction (transitions are on whole frames) | Passed |
| 2c validate | ops that are not a list is refused | Passed |
| 2c validate | a revision id that is a path is refused | Passed |
| 2c validate | a proposal id that is a path is refused | Passed |
| 2c validate | apply re-runs the operations: a hand-edited proposal file is refused | Passed |
| 2c validate | and the head did not move | Passed |
| 2d match cut | an insert inside a match cut is refused | Passed |
| 2d match cut | removing half of a match cut is refused | Passed |
| 2d match cut | moving half of a match cut away is refused | Passed |
| 2d match cut | a match cut stays a hard cut | Passed |
| 2d match cut | a match cut's edge is not trimmed | Passed |
| 2d first clip | a move that leaves a dissolve next to a clip too short for it is refused (c004's, now after c002) | Passed |
| 2d first clip | removing the first clips is accepted (the new first clip loses its dissolve) | Passed |
| 3 roll | a 3-frame roll is refused: c004 would be too short for its 19-frame dissolve | Passed |
| 3 roll | a 1-frame roll moves exactly two edges | Passed |
| 3 roll | a roll keeps the running time | Passed |
| 3 roll | clips not named are unchanged | Passed |
| 3 roll | proposal accepted | Passed |
| 4 replace | c004 is replaced by a new clip of shot 4, same length | Passed |
| 4 replace | the proposal renders before it is applied (a preview), and the head stays | Passed |
| 4 replace | proposal accepted | Passed |
| 4 replace | the replacement keeps c004's incoming dissolve | Passed |
| 5 transition | a dissolve longer than 40% of the shorter clip is refused | Passed |
| 5 transition | junction 4 becomes a 6-frame dissolve | Passed |
| 5 transition | proposal accepted | Passed |
| 5 transition | the dissolve starts c005 6 frames earlier on the record (a dissolve overlaps its clips): later cuts ripple | Passed |
| 6 beat | so junction 4, on the beat before, is now off it | Passed |
| 6 beat | junction 3 (made a frame later by the roll) is rolled onto its beat | Passed |
| 6 beat | proposal accepted | Passed |
| 6 beat | only junction 3 moved (a roll ripples nothing) | Passed |
| 6 beat | snapping a junction already on its beat changes nothing | Passed |
| 7 batch | an insert and a dip to black in one proposal | Passed |
| 7 batch | proposal accepted | Passed |
| 7 batch | five revisions r001..r005 | Passed |
| 8 undo | undo moves the head to r004 | Passed |
| 8 undo | redo comes back to r005 | Passed |
| 8 branch | a cut, a move, a remove and a slip in one proposal, each reported | Passed |
| 8 branch | an edit after an undo makes a branch (r006 from r004); r005 is kept | Passed |
| 8 branch | a proposal made before the head moved is refused | Passed |
| 8 branch | redo from r004 takes the latest branch | Passed |
| 8 branch | checkout moves the head back to the r005 branch | Passed |
| 8 undo | the log lists every revision with its parent | Passed |
| 9 compare | r000 -> r004 shows the net effect: the replace, the new dissolve, and the roll and beat snap combined into one edge move | Passed |
| 10 render | the render has exactly the cut's frames | Passed |
| 10 render | audio and video lengths agree within 2 frames | Passed |
| 10 render | r004 (every clip in source order) has audio as long as its video | Passed |
| 10 render | every decisive clip shows its exact source frames (offset 0, three frames each) | Passed |
| 10 render | a junction's review window renders | Passed |
| 10 render | the review window of an out-of-order junction has audio as long as its video | Passed |
| 11 publish | publishing over an editor's import is refused | Passed |
| 11 publish | with replace_import the editor's import is moved aside, not lost | Passed |
| 11 publish | the head becomes the human stage, not stale | Passed |
| 11 publish | the editor pin is still applied in the human stage | Passed |
| 11 publish | matchcut junctions lists the human stage (5 junctions; the new clips renamed h001, h002 by M2a's builder) | Passed |
| 11 publish | M2a's diff of the published stage finds the clips but reads the two new transitions on c005 as trims (its neighbours changed) | Passed |
| 11 publish | matchcut export refuses the human stage (M2a's rule: only canonical stages go out), so an edited cut cannot yet go to an NLE | Known gap (not a pass) |
| 12 mcp | initialize and tools/list (11 tools) | Passed |
| 12 mcp | edit_inspect over MCP equals the Python call | Passed |
| 12 mcp | a refusal over MCP carries its reason | Passed |
| 12 mcp | an error is a tool result with isError | Passed |
| 12 mcp | a malformed request is an isError result and the server keeps answering | Passed |
| 12 mcp | the JSON CLI serves the same tools | Passed |
| 13 stale | the session sees that final changed | Passed |
| 13 stale | a stale session refuses to propose | Passed |
| 13 stale | the published human stage is stale too (the engine's own rule) | Passed |
| 13 stale | a new session moves the old one aside (kept) and starts at r000 | Passed |
| 14 determinism | the same edits on a fresh copy make the same revisions, byte for byte | Passed |
| 14 determinism | every check of the second run passed | Passed |

## Frame accuracy (r005, rendered at source size)

| Clip | Shot | Record frames | Source frames | Best offsets | Decisive |
|---|---|---|---|---|---|
| c001 | 0 | [24, 25, 26] | [24, 25, 26] | [-2, -2, -2] | no (static) |
| c002 | 1 | [57, 58, 59] | [111, 112, 113] | [0, 0, 0] | yes |
| c003 | 3 | [87, 88, 89] | [187, 188, 189] | [-2, -2, -2] | no (static) |
| e001 | 4 | [127, 128, 129] | [263, 264, 265] | [0, 0, 0] | yes |
| c005 | 7 | [152, 153, 154] | [452, 453, 454] | [0, 0, 0] | yes |
| e002 | 5 | [169, 170, 171] | [338, 339, 340] | [-2, -2, -2] | no (static) |

## Timings (seconds, this container)

- workspace (mock pipeline, twice): 7.54
- start: 0.0017
- inspect: 0.0007
- propose roll: 0.0084
- apply: 0.0032
- render full (r005, source size): 1.4895
- render junction window (r005, j3): 1.4315
- render again (cached): 0.0008
- mcp: 7 requests incl. start-up: 0.337
