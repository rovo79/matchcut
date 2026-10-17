"""The junction inspector's text: badges, the score comparison, cut points, why and history."""
from __future__ import annotations

from typing import List, Optional

from rich.text import Text

from matchcut.pipeline.editorial import Pin
from matchcut.pipeline.junctions import SCORE_ROWS, Junction, JunctionState, frames_delta

KEYS = "l lock these points · v revert to the first cut · x clear pin · o open blend · Enter full history"


def badges(j: Junction, shown: JunctionState, pin: Optional[Pin]) -> List[str]:
    out: List[str] = []
    if j.match_type:
        out.append(f"MATCH CUT ({j.match_type})")
    if shown.locked:
        out.append("locked by the editor" if j.pinned else "locked by matchcut")
    if j.beat_boundary:
        out.append("beat boundary")
    if shown.score is not None and shown.score.jump_cut_risk:
        out.append("jump-cut risk")
    if pin is not None:
        out.append(f"pinned ({pin.origin})")
        tol = (1.0 / j.fps if j.fps > 0 else 0.04) + 1e-6
        if abs(shown.out_time - pin.out_time) > tol or abs(shown.in_time - pin.in_time) > tol or not shown.locked:
            out.append("pin pending: run again (R) to apply")
    return out


def render_inspector(j: Junction, stage: str, pin: Optional[Pin], expanded: bool = False) -> Text:
    shown = j.state_at(stage) or j.current
    base = j.baseline
    t = Text()
    t.append(f"Junction {j.index + 1} · {j.out_clip} → {j.in_clip} · shot {j.out_shot} → shot {j.in_shot} · beat {j.out_beat} → {j.in_beat}\n", style="bold")
    marks = badges(j, shown, pin)
    t.append((" · ".join(marks) if marks else "no match cut, lock or pin on this junction") + "\n", style="yellow" if marks else "dim")
    if base.score is None or shown.score is None:
        t.append("scores unavailable: the workspace has no analysis.npz\n", style="dim")
    else:
        t.append(f"{'':<13}{base.stage:>10}{shown.stage:>10}{'Δ':>8}\n", style="dim")
        for name in SCORE_ROWS:
            v0, v1 = getattr(base.score, name), getattr(shown.score, name)
            t.append(f"{name:<13}{v0:>10.2f}{v1:>10.2f}{v1 - v0:>+8.2f}\n", style="bold" if name == "combined" else "")
        t.append(f"motion type  {shown.score.motion_type}\n", style="dim")
    t.append(
        f"cut points   out {base.out_time:.2f}s → {shown.out_time:.2f}s [{frames_delta(shown.out_time - base.out_time, j.fps)}]"
        f"   in {base.in_time:.2f}s → {shown.in_time:.2f}s [{frames_delta(shown.in_time - base.in_time, j.fps)}]\n"
    )
    t.append("why: ", style="bold")
    t.append(j.why() + "\n")
    if expanded:
        t.append("history:\n", style="bold")
        for line in j.history():
            t.append(f"  {line}\n")
        t.append("decisions:\n", style="bold")
        for d in j.decisions:
            t.append(f"  {d.pass_name}:{d.action}  {d.detail}\n")
    else:
        t.append("history: ", style="bold")
        t.append(" · ".join(j.history()) + "\n")
    t.append(KEYS, style="dim")
    return t
