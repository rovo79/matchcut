# Source-first discovery: Phase 1 audit

Read from the code at main 47404f8. Nothing here was run on Fallingwater footage; that footage is not available in the cloud session.

## Two separate systems

| | Research code | Editing pipeline |
|---|---|---|
| Where | `matchcut/{data,models,experiments}.py`, `experiment.py`, `data/*.json` | `matchcut/pipeline/*` |
| Input | The Netflix-style labelled dataset: pre-computed per-clip embeddings, human match / no-match labels | Your own footage, decoded with ffmpeg |
| Method | Trained classifiers (logistic regression, XGBoost, MLP) and metric learning (`pytorch_metric_learning`) on embedding pairs | Handcrafted numpy descriptors and a fixed weighted sum |
| Operates on user footage? | **No.** It scores dataset pairs and writes `experiment1.csv` / `experiment2.csv`. | Yes |
| Needs | torch, sklearn, xgboost (these are the two test collection errors) | numpy, ffmpeg |

The trained, paper-style ranking has never been connected to the footage pipeline.

## What the pipeline does (verified by reading the code)

- **Representation.** Footage is decoded once at low resolution. Features are computed per 0.25 s "cell" (`cell_seconds`):
  - a 32×18 luminance thumbnail, zero-mean and unit-norm (structure);
  - edge-orientation histograms (composition);
  - luminance and a 4³ RGB histogram (colour);
  - optical-flow-style motion over ±2 frames: magnitude, speed, mean direction, and a 3×3 spatial grid.
- **Shot boundaries.** Colour-histogram and pixel-difference series with rolling median/MAD thresholds (`shots.py`). This is the only thing that splits a MOV into pieces. There is no moment index below shot level.
- **Scoring (`matching.py`).**
  - `frame` = weighted sum of structure cosine, edge L1, luminance and colour intersection.
  - `motion` is direction cosine, magnitude and location when both sides move. It is a constant 0.5 if both are static and 0.2 if only one moves.
  - `combined` = `w_frame*frame + w_motion*motion`.
  - A jump-cut flag is raised when structure and colour are both near-identical.
  - These are handcrafted metrics with a **fixed weighted score**, which is what the brief says not to rely on.
- **Search for cut points.** `MatchScorer.best_junction` does search in/out points, but only inside a **small trim window** (`trim_slack_seconds`) around the cut that the rough cut already chose (`MatchCutPass`). It evaluates a few cell pairs per junction. It does not search the source for new pairs.
- **Verification.** Optionally an LLM looks at frame pairs (`verify_with_llm`, `llm.py`) and returns match_cut, jump_cut or no. This is the only semantic judgement, and it needs an API backend. Without one, a candidate is accepted on feature score alone.
- **Where editorial decisions come from.** The rough cut (which clips, order, durations) comes from the LLM narrative step or from hand-supplied pins in `editorial.json`. Match pairs are discovered only among clips already chosen. The earlier films' matches were found that way, or by hand.

## Capability matrix

| Netflix-inspired capability | State |
|---|---|
| Frame similarity (handcrafted) | Operational, used by `MatchCutPass` |
| Motion similarity (handcrafted flow descriptors) | Operational, crude. Mean direction only, with no trajectories or camera/object separation |
| Learned frame/motion embeddings and trained ranker | Present only in the research code, on dataset embeddings. Dormant for footage |
| Corpus-wide pair search (any moment against any moment) | **Absent** |
| Moment-level index inside a shot or MOV | **Absent** (shot level only) |
| Directional A→B vs B→A | Partly. `score_cut(out, in)` is directional, but it is never run over all pairs |
| Pretrained image/video encoders on footage | **Absent** |
| Semantic or analogy matching | Only through the optional LLM check |

## What this means for the experiment

Phases 2–4 need new code, not configuration:

1. a moment index (temporal sampling plus change detection) over all MOVs, with proxies for analysis and originals kept for rendering;
2. four independent scorers, one per flavour, with no merged score;
3. an all-pairs, directional, windowed cut-point search that reuses `MatchScorer`'s descriptors where they are adequate.

Reusable as is: ffmpeg frame decode (`media.py`), cell features and the motion descriptors, the shot detector, and the render and review tools. They would be used as a baseline flavour (composition / motion), not as the final answer.

## Blocker for Phases 2–6

The original Fallingwater MOVs are not in this environment. A filesystem search found only synthetic test clips from earlier review runs. The footage lives on your Mac, and this session cannot reach it. See the accompanying message for options.
