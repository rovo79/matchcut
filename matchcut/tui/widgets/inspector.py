"""The junction inspector's text: badges, the score comparison, cut points, why and history."""
from __future__ import annotations

from typing import List, Optional

from rich.text import Text

from matchcut.pipeline.editorial import Pin
from matchcut.pipeline.junctions import SCORE_ROWS, Junction, JunctionState, frame_delta, signed
from matchcut.pipeline.timeline import Decision


def _same_points(a: float, b: float) -> bool:
    return round(a, 3) == round(b, 3)


def _pin_badge(j: Junction, pin: Pin, decision: Optional[Decision]) -> str:
    """Where the pin stands, judged by the editor's decision in the latest stage's history (``None`` when the latest run has none for this pin)."""
    if decision is None or not (
        _same_points(float(decision.data.get("out_time", float("nan"))), pin.out_time)
        and _same_points(float(decision.data.get("in_time", float("nan"))), pin.in_time)
    ):
        return "pin pending: run again (R) to apply"
    if decision.action == "pin_unplaced":
        return f"pin not placed: {decision.detail}"
    if decision.action == "pin_changed":  # the human stage: the editor's cut no longer holds the pin
        return f"pin changed by the editor: {decision.detail}"
    if decision.action != "pinned":
        return "pin pending: run again (R) to apply"
    tol = (1.0 / j.fps if j.fps > 0 else 0.04) + 1e-6
    placed_out, placed_in = float(decision.data.get("out", pin.out_time)), float(decision.data.get("in_", pin.in_time))
    check = ", piece chosen by nearest boundary: check it" if decision.data.get("by_boundary") else ""
    if abs(placed_out - pin.out_time) <= tol and abs(placed_in - pin.in_time) <= tol:
        return f"pinned ({pin.origin}){check}"
    return f"pinned ({pin.origin}), adjusted to out {placed_out:.2f}s / in {placed_in:.2f}s to fit the clips{check}"


def badges(j: Junction, shown: JunctionState, pin: Optional[Pin], pin_decision: Optional[Decision] = None) -> List[str]:
    out: List[str] = []
    if j.match_type and shown.locked:
        out.append(f"MATCH CUT ({j.match_type})")
    if shown.locked and not j.pin_lost:  # (a pin the editor's cut no longer holds says so in the pin badge; the lock was the pin's)
        if j.pinned:
            out.append(f"{j.pin_origin or 'locked'} by the editor")
        else:
            out.append("locked by matchcut")
    if j.beat_boundary:
        out.append("beat boundary")
    if shown.score is not None and shown.score.jump_cut_risk:
        out.append("jump-cut risk")
    if pin is not None:
        out.append(_pin_badge(j, pin, pin_decision))
    return out


def render_inspector(j: Junction, stage: str, pin: Optional[Pin], expanded: bool = False, pin_decision: Optional[Decision] = None) -> Text:
    shown = j.state_at(stage) or j.current
    base = j.baseline
    t = Text()
    t.append(f"Junction {j.index + 1} · {j.out_clip} → {j.in_clip} · shot {j.out_shot} → shot {j.in_shot} · beat {j.out_beat} → {j.in_beat}\n", style="bold")
    marks = badges(j, shown, pin, pin_decision)
    t.append((" · ".join(marks) if marks else "no match cut, lock or pin on this junction") + "\n", style="yellow" if marks else "dim")
    if base.score is None or shown.score is None:
        t.append("scores unavailable (no usable analysis for this workspace)\n", style="dim")
    else:
        single = shown.stage == base.stage
        if single:
            t.append(f"{'':<13}{shown.stage:>10}\n", style="dim")
        else:
            t.append(f"{'':<13}{base.stage:>10}{shown.stage:>10}{'Δ':>8}\n", style="dim")
        for name in SCORE_ROWS:
            v0, v1 = getattr(base.score, name), getattr(shown.score, name)
            row = f"{name:<13}{v1:>10.2f}" if single else f"{name:<13}{v0:>10.2f}{v1:>10.2f}{signed(v1 - v0):>8}"
            t.append(row + "\n", style="bold" if name == "combined" else "")
        t.append(f"motion type  {shown.score.motion_type}\n", style="dim")
    t.append(
        f"cut points   out {base.out_time:.2f}s → {shown.out_time:.2f}s [{frame_delta(base.out_time, shown.out_time, j.fps)}]"
        f"   in {base.in_time:.2f}s → {shown.in_time:.2f}s [{frame_delta(base.in_time, shown.in_time, j.fps)}]\n"
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
    t.rstrip()
    return t
