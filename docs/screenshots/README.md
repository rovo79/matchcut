# Edit tab screenshots

Captured headless with `python3 tools/capture_edit.py WORKSPACE docs/screenshots --junctions 3`
(a 140x46 terminal, half-block images). Both are the final cut of the 20 s Big
Buck Bunny clip from a mock-backend run (`-t 12 --no-render`): the frames and the
scores are real, the descriptions and narrative are mock, and the status bar says
so. The clip has three shots, so there are two junctions and the tool writes two
files.

- `edit-junction-1.svg`: junction 1 (shot 0 to shot 1), a beat boundary with a 0.75 s dissolve: outgoing frame, incoming frame, 50% overlay, scores, cut points, why and history.
- `edit-junction-2.svg`: junction 2 (shot 1 to shot 2), one `→` later, with the same layout.

Big Buck Bunny © Blender Foundation, CC BY 3.0 (peach.blender.org)
