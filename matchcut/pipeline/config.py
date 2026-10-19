"""Configuration for the AI editing pipeline.

Every knob the pipeline exposes lives here so that a run is fully described by
one serialisable object (it is written to ``workspace/config.json``).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PASSES: Tuple[str, ...] = (
    "matchcut",
    "continuity",
    "pacing",
    "transitions",
    "polish",
    "beatsync",
)


@dataclass
class AnalysisConfig:
    """How the footage is decoded and sampled for analysis."""

    width: int = 160  # analysis resolution (low-res, fast)
    height: int = 90
    fps: float = 8.0  # analysis frame rate
    cell_seconds: float = 0.25  # feature cell stride
    motion_window: int = 2  # frames on each side of a cell centre for motion
    audio_sample_rate: int = 8000
    audio_hop_seconds: float = 0.5


@dataclass
class ShotConfig:
    """Shot boundary detection."""

    hist_threshold: float = 0.25  # absolute chi-square threshold on colour hists
    pixel_threshold: float = 0.45  # absolute mean-abs-diff threshold (0..1)
    max_structure_similarity: float = 0.85  # above this the layout is unchanged => fade, not a cut
    adaptive_k: float = 6.0  # boundary must exceed local median + k * MAD
    adaptive_window: int = 24  # frames on each side for the local statistics
    min_shot_seconds: float = 0.5
    max_shots: int = 1500  # safety valve for very long footage


@dataclass
class SamplingConfig:
    """How keyframes are sampled and packed for the model."""

    frames_per_shot: int = 3  # head / middle / tail
    tile_width: int = 256
    tile_height: int = 144
    shots_per_sheet: int = 8
    sheets_per_request: int = 3
    pairs_per_sheet: int = 6
    storyboard_columns: int = 4
    jpeg_quality: int = 82


@dataclass
class MatchCutConfig:
    """Scoring and search for match cuts."""

    w_frame: float = 0.7
    w_motion: float = 0.3
    w_structure: float = 0.45
    w_composition: float = 0.30
    w_luminance: float = 0.10
    w_color: float = 0.15
    candidate_threshold: float = 0.55  # score needed to be a candidate
    strong_threshold: float = 0.72  # considered strong even without LLM
    moving_energy: float = 0.02  # mean abs frame diff that counts as motion
    moving_speed: float = 0.75  # px/frame (analysis res) that counts as motion
    jump_cut_structure: float = 0.96  # near-identical frames => jump cut risk
    jump_cut_color: float = 0.95
    trim_slack_seconds: float = 1.5  # how far in/out points may move
    allow_reorder: bool = True
    reorder_min_gain: float = 0.15
    max_llm_pairs: int = 48  # cap on pairs sent for verification
    verify_with_llm: bool = True


@dataclass
class PacingConfig:
    min_clip_seconds: float = 0.75
    max_clip_seconds: float = 8.0
    pace_targets: Dict[str, float] = field(
        default_factory=lambda: {"slow": 5.0, "medium": 3.0, "fast": 1.6}
    )
    duration_tolerance: float = 0.08  # +-8% of target total duration


@dataclass
class TransitionConfig:
    dissolve_seconds: float = 0.75
    dip_to_black_seconds: float = 1.0
    fade_in_seconds: float = 1.0
    fade_out_seconds: float = 1.5
    max_fraction_of_clip: float = 0.4  # transition may not eat more than this


@dataclass
class PolishConfig:
    max_edits: int = 8
    storyboards_per_request: int = 3


@dataclass
class MusicConfig:
    """Prerecorded music track played under the cut."""

    path: Optional[str] = None  # None => no music
    start: float = 0.0  # seconds into the track where the cut begins
    gain_db: float = -8.0  # music level relative to the source audio
    duck: bool = True  # sidechain-duck the music under the source audio
    duck_threshold: float = 0.05  # source level (0..1) above which ducking kicks in
    duck_ratio: float = 6.0
    only: bool = False  # drop the source audio entirely
    fade_out: float = 2.0  # seconds of music fade at the end of the cut
    loudnorm: bool = True  # EBU R128 normalisation of the final mix
    target_lufs: float = -16.0
    sample_rate: int = 22050  # analysis sample rate
    hop_seconds: float = 0.5  # energy curve resolution
    beats_per_bar: int = 4
    snap: bool = True  # move cuts onto beats (beatsync pass)
    snap_window: float = 0.35  # max seconds a cut may move
    prefer_downbeats_at_beat_changes: bool = True


@dataclass
class LLMConfig:
    backend: str = "anthropic"  # "anthropic" | "mock"
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 16000
    max_retries: int = 2
    refusal_fallbacks: bool = True
    timeout_seconds: float = 600.0
    thinking_display: str = "omitted"


@dataclass
class RenderConfig:
    enabled: bool = True
    width: Optional[int] = None  # None => source resolution
    crf: int = 20
    preset: str = "medium"
    audio_bitrate: str = "192k"
    proxy: bool = False  # quick low-res preview render
    proxy_width: int = 640


@dataclass
class ReviewConfig:
    """Temporal editorial review in mpv (``matchcut.review``)."""

    pre_seconds: float = 2.0  # shown before the cut
    post_seconds: float = 2.0  # shown after the transition
    width: int = 0  # width of rendered windows; 0 = the source resolution, so a rendered window matches an EDL window pixel for pixel
    crf: int = 23  # quality of rendered windows
    mpv: str = "mpv"  # the mpv binary
    always_render: bool = False  # render every window instead of playing hard cuts from an EDL


@dataclass
class PipelineConfig:
    source: str = ""
    workspace: str = "workspace"
    brief: str = ""  # creative direction from the user
    target_duration: Optional[float] = None  # seconds; None => model decides
    passes: Tuple[str, ...] = DEFAULT_PASSES
    force: bool = False  # ignore cached stage outputs
    seed: int = 0
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    shots: ShotConfig = field(default_factory=ShotConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    matchcut: MatchCutConfig = field(default_factory=MatchCutConfig)
    pacing: PacingConfig = field(default_factory=PacingConfig)
    transitions: TransitionConfig = field(default_factory=TransitionConfig)
    polish: PolishConfig = field(default_factory=PolishConfig)
    music: MusicConfig = field(default_factory=MusicConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)

    # ------------------------------------------------------------------ io
    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineConfig":
        kwargs: Dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            section_type = _section_type(f)
            if section_type is not None and isinstance(value, dict):
                value = _dataclass_from_dict(section_type, value)
            elif f.name == "passes":
                value = tuple(value)
            kwargs[f.name] = value
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Path) -> "PipelineConfig":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def apply_overrides(self, overrides: List[str]) -> None:
        """Apply ``section.key=value`` overrides (values parsed as JSON when possible)."""
        for item in overrides:
            if "=" not in item:
                raise ValueError(f"override must look like section.key=value, got {item!r}")
            key, raw = item.split("=", 1)
            try:
                value: Any = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            target: Any = self
            parts = key.split(".")
            for part in parts[:-1]:
                target = getattr(target, part)
            if not hasattr(target, parts[-1]):
                raise ValueError(f"unknown config key {key!r}")
            setattr(target, parts[-1], value)


def _section_type(f: "dataclasses.Field[Any]") -> Optional[type]:
    """Return the dataclass type of a nested config section, if the field is one."""
    factory = f.default_factory
    if factory is dataclasses.MISSING:
        return None
    if isinstance(factory, type) and dataclasses.is_dataclass(factory):
        return factory
    return None


def _dataclass_from_dict(cls: type, data: Dict[str, Any]) -> Any:
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})
