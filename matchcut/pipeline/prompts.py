"""Prompts for every model call. Kept in one place so they are easy to tune."""

EDITOR_SYSTEM = """You are a senior film editor and story consultant.
You work from sampled footage: contact sheets of keyframes and short machine-extracted
notes. You think in beats, rhythm and visual continuity. You are precise about shot ids
and timings, and you never invent shots that are not in the catalog.
Answer only with the requested JSON."""

DESCRIBE = """Each contact sheet row is one shot from the footage. Left to right the tiles are
the head, middle and tail of the shot (timestamps shown). Shots on these sheets: {shot_ids}.

For every shot id listed above produce one entry. Be concrete and visual: name the subject,
its placement in the frame, the dominant shapes and lines, how the camera and subject move,
and the single key action. Mark a shot unusable only if it is black, hopelessly blurred, or
clearly a slate/junk. Machine notes per shot (duration, brightness, motion energy and
dominant motion direction from optical analysis) are:
{machine_notes}"""

NARRATIVE = """Find the visual narrative in this footage and propose a beat structure for a cut.

Creative brief from the director (may be empty): {brief}
Target running time: {target}
{music}
Footage summary: {n_shots} shots, {total_duration:.1f}s of source material, in source order.

Shot catalog (JSON, one entry per shot in source order):
{catalog}

Guidance:
- Think like a story editor: what is the emotional arc the images support? Find the thread
  (a subject, a place, a day, a transformation) and build 3-7 beats around it.
- Every beat gets candidate shot ids, best first. A shot may appear in at most one beat.
- Prefer shots with clear subjects and movement; keep unusable shots out.
- Pace: 'fast' beats favour short clips, 'slow' beats let shots breathe.
- target_duration_s across beats should sum to approximately the target running time.
- transition_in is the transition that leads INTO the beat ('cut' for the first beat)."""

ROUGH_CUT = """Make the rough cut: an ordered list of clips following the beat structure below.

Creative brief: {brief}
Target running time: {target}
{music}
Narrative:
{narrative}

Shot catalog (JSON). 'start'/'end' are the shot's source bounds in seconds; a clip's in_s/out_s
must stay inside them.
{catalog}

Rules:
- Clips play in the order you list them. Group them by beat, beats in the order given.
- Each clip: {min_clip:.2f}s to {max_clip:.1f}s long. Choose the most expressive section of
  the shot (the machine 'interest' curve hints where motion happens).
- Use a shot at most once. Not every candidate shot has to be used.
- Think about visual flow between consecutive clips: alternate shot sizes, carry movement
  direction, land on similar shapes and screen positions where you can - these are the seeds
  for match cuts that later passes will tighten.
- Respect the target running time (sum of clip durations)."""

VERIFY_PAIRS = """Each row on the sheets is a proposed cut: LEFT tile is the last frame of the outgoing
clip, RIGHT tile is the first frame of the incoming clip. Judge each junction as an editor:

- match_cut: composition, shape, screen position, movement or action genuinely carries
  across the cut (graphic match, motion match, or action match). Different subject or
  location but rhyming image.
- acceptable_cut: a clean, ordinary cut. Nothing rhymes, nothing jars.
- jump_cut: same subject/setup on both sides with only a small change - it will read as a
  glitch rather than a cut.
- bad_cut: the junction is jarring (crossing the line, mismatched motion, confusing).

Machine scores for each pair (frame similarity, motion similarity, combined, 0..1):
{pair_notes}

Pairs to judge: {pair_ids}. Return one verdict per pair id."""

POLISH = """Review the current cut and propose final polish edits.

Creative brief: {brief}
Target running time: {target}
Narrative:
{narrative}

The storyboard sheet shows the first frame of every clip in timeline order with its clip id,
shot number, duration and beat. The cut list (JSON) gives exact timings, transitions and the
match-cut decisions already made:
{cut_list}

Consider: rhythm within and between beats, whether the opening and closing images land,
redundant clips, clips that overstay, moments that need air, and whether the transitions
match the emotional shifts. Clips with 'locked' in/out points carry verified match cuts -
do not trim those ends.

Propose at most {max_edits} edits. Each edit must be small and concrete (trim/extend by a
number of seconds, drop a clip, move a clip before another, change a transition). Use 'keep'
with an empty list of other edits if the cut is already right."""
