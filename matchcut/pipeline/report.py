"""Human-readable report of a run."""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from .context import Context
from .timeline import Timeline


def _fmt(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"


def build_report(ctx: Context, final: Timeline, rough: Optional[Timeline] = None, passes: Optional[Dict[str, Timeline]] = None) -> str:
    cfg = ctx.cfg
    n = ctx.narrative
    lines: List[str] = []
    lines.append(f"# Edit report: {ctx.media.path}")
    lines.append("")
    lines.append(f"- Source: {ctx.media.width}x{ctx.media.height} @ {ctx.media.fps:.3f} fps, {_fmt(ctx.media.duration)}, {len(ctx.shots)} shots")
    lines.append(f"- Brief: {cfg.brief or '(none)'}")
    lines.append(f"- Target running time: {cfg.target_duration or 'model decided'}")
    lines.append(f"- Final cut: {len(final.clips)} clips, {_fmt(final.duration)}")
    lines.append(f"- Model backend: {cfg.llm.backend} ({cfg.llm.model}), {ctx.llm.calls} calls, usage {ctx.llm.usage}")
    lines.append("")

    if n:
        lines.append("## Visual narrative")
        lines.append("")
        lines.append(f"**Logline.** {n.get('logline', '')}")
        lines.append("")
        lines.append(f"**Theme.** {n.get('theme', '')}")
        lines.append("")
        lines.append(n.get("structure_notes", ""))
        lines.append("")
        lines.append("| Beat | Title | Intent | Tone | Pace | Target | Shots |")
        lines.append("|---|---|---|---|---|---|---|")
        for b in n.get("beats", []):
            lines.append(
                f"| {b['id']} | {b['title']} | {b['intent']} | {b['emotional_tone']} | {b['pace']} | {b['target_duration_s']:.1f}s | {', '.join(map(str, b['shot_ids']))} |"
            )
        lines.append("")

    lines.append("## Final cut list")
    lines.append("")
    lines.append("| # | Record in | Clip | Shot | Beat | Source in-out | Dur | Transition | Match | Subject |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(final.clips):
        match = ", ".join(t.split(":", 1)[1] + ("←" if t.startswith("match_in") else "→") for t in c.tags if t.startswith("match_"))
        tr = c.transition_in.kind if c.transition_in.is_cut is False else "cut"
        subject = ctx.descriptions.get(c.shot_index, {}).get("subject", "")
        lines.append(
            f"| {i + 1} | {_fmt(final.record_start(i))} | {c.id} | {c.shot_index} | {c.beat_id} | {c.source_in:.2f}-{c.source_out:.2f} | {c.duration:.2f}s | {tr} | {match} | {subject} |"
        )
    lines.append("")

    lines.append("## Pass decisions")
    lines.append("")
    by_pass: Dict[str, List[Any]] = {}
    for d in final.history:
        by_pass.setdefault(d.pass_name, []).append(d)
    for name, ds in by_pass.items():
        counts = Counter(d.action for d in ds)
        lines.append(f"### {name} ({', '.join(f'{k}: {v}' for k, v in counts.items())})")
        lines.append("")
        for d in ds:
            score = f" [{d.score:.2f}]" if d.score is not None else ""
            clips = ", ".join(d.clip_ids[:6]) + (" …" if len(d.clip_ids) > 6 else "")
            lines.append(f"- **{d.action}** {clips}{score}: {d.detail}")
        lines.append("")

    if passes:
        lines.append("## Timeline evolution")
        lines.append("")
        lines.append("| Stage | Clips | Duration |")
        lines.append("|---|---|---|")
        if rough is not None:
            lines.append(f"| rough cut | {len(rough.clips)} | {_fmt(rough.duration)} |")
        for name, tl in passes.items():
            lines.append(f"| {name} | {len(tl.clips)} | {_fmt(tl.duration)} |")
        lines.append("")
    return "\n".join(lines)
