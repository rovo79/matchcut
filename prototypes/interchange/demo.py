"""Feasibility-proof demo (not part of matchcut): write interchange files for a finished workspace and print junction timecodes.

    python3 prototypes/interchange/demo.py WORKSPACE OUTDIR [--junction N]

Writes roughcut.otio, final.otio, matchcut.fcpxml (FCPXML 1.10, one project per stage), provenance.json (canonical sidecar),
final.edl (mpv EDL, hard cuts only) and resolve_open_junction.py (for a Resolve Studio you run yourself; untested against a live Resolve).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import interchange as I  # noqa: E402
from matchcut.pipeline.config import PipelineConfig  # noqa: E402
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines, scorer_for_workspace  # noqa: E402


def run(ws: Path, out: Path, junction: int = -1) -> None:
    ws, out = Path(ws), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    stages = load_stage_timelines(ws)
    names = dict(stages)
    if "roughcut" not in names or "final" not in names:
        raise SystemExit(f"{ws} needs roughcut.json and final.json")
    try:
        scorer = scorer_for_workspace(ws, PipelineConfig.load(ws / "config.json"))
    except Exception:
        scorer = None
    probe = json.loads((ws / "probe.json").read_text()) if (ws / "probe.json").exists() else {}
    final = names["final"]
    media = final.source if Path(final.source).exists() else probe.get("path", final.source)
    fr = I.rational_fps(final.fps)
    jb = {n: build_junctions(stages, scorer, upto=n) for n, _ in stages}
    sel = [(n, t) for n, t in stages if n in ("roughcut", "final")]
    kw = dict(media_duration=probe.get("duration"))
    for stage, doc in I.export_otio(sel, media, fr, jb, audio=bool(probe.get("has_audio", True)), **kw).items():
        (out / f"{stage}.otio").write_text(json.dumps(doc, indent=1))
    (out / "matchcut.fcpxml").write_text(I.export_fcpxml(sel, media, fr, jb, size=(probe.get("width", 1920), probe.get("height", 1080)),
                                                         has_audio=bool(probe.get("has_audio", True)), **kw))
    I.write_provenance(out / "provenance.json", stages, jb, media, fr)
    (out / "final.edl").write_text(I.mpv_edl(final, media))

    js = jb["final"]
    jids = I.junction_ids(js)
    frames = {jid: I.junction_time(final, j.index)[1] for j, jid in zip(js, jids)}
    pick = junction if junction >= 0 else next((j.index for j in js if final.clips[j.index + 1].transition_in.kind != "cut"), 0)
    notes = {jid: (f"J{j.index + 1} s{j.out_shot}->s{j.in_shot}", I._trunc(j.why())) for j, jid in zip(js, jids)}
    (out / "resolve_open_junction.py").write_text(I.resolve_script(str(out / "final.otio"), f"matchcut final ({Path(media).stem})", frames,
                                                                   jids[pick], fps=final.fps, notes=notes))
    print(f"{ws} -> {out}  ({len(final.clips)} clips, {final.fps:g} fps = {fr[0]}/{fr[1]}, record {final.duration:.3f}s, mpv EDL {sum(c.duration for c in final.clips):.3f}s)")
    print(f"{'J':>2} {'id':<8} {'cut frame':>9} {'timeline TC':>12} {'mpv EDL s':>9}  {'transition':<18} why")
    for j, jid in zip(js, jids):
        c = final.clips[j.index + 1]
        tr = "cut" if c.transition_in.is_cut else f"{c.transition_in.kind} {c.transition_in.duration:.2f}s"
        _, frame, tc = I.junction_time(final, j.index)
        print(f"{j.index + 1:>2} {jid:<8} {frame:>9} {tc:>12} {I.junction_time(final, j.index, edl=True)[0]:>9.2f}  {tr:<18} {I._trunc(j.why(), 70)}"
              + ("   <- resolve_open_junction.py" if j.index == pick else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("outdir")
    ap.add_argument("--junction", type=int, default=-1, help="0-based junction for the Resolve playhead (default: first with a transition)")
    a = ap.parse_args()
    run(Path(a.workspace), Path(a.outdir), a.junction)
