"""Structured-output schemas for every model call (pydantic)."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- describe --------------------------------------------------------------
ShotSize = Literal["extreme_wide", "wide", "medium", "close_up", "extreme_close_up", "insert", "unknown"]
CameraMove = Literal["static", "pan", "tilt", "dolly", "handheld", "zoom", "crane", "unknown"]
Motion = Literal["none", "left", "right", "up", "down", "toward", "away", "complex"]


class ShotDescription(Strict):
    shot_id: int
    description: str = Field(description="One or two sentences: what we see and what happens.")
    subject: str = Field(description="Main subject, e.g. 'woman at window', 'spinning wheel'.")
    setting: str
    shot_size: ShotSize
    camera_movement: CameraMove
    subject_motion: Motion
    dominant_shapes: str = Field(description="Shapes/lines that dominate the frame, e.g. 'circle centre', 'strong diagonal'.")
    mood: str
    tags: List[str]
    key_action: str = Field(description="The single most important action or moment, or 'none'.")
    quality: float = Field(description="0..1 technical/aesthetic usability.")
    usable: bool


class DescribeResponse(Strict):
    shots: List[ShotDescription]


# -- narrative -------------------------------------------------------------
Pace = Literal["slow", "medium", "fast"]
TransitionKind = Literal["cut", "dissolve", "dip_to_black"]


class Beat(Strict):
    id: str = Field(description="Short id like 'b1'.")
    title: str
    intent: str = Field(description="What this beat must accomplish for the viewer.")
    emotional_tone: str
    pace: Pace
    target_duration_s: float
    shot_ids: List[int] = Field(description="Candidate shots for this beat, best first.")
    transition_in: TransitionKind


class NarrativeResponse(Strict):
    logline: str
    theme: str
    structure_notes: str
    beats: List[Beat]
    unused_shot_ids: List[int]
    rationale: str


# -- rough cut -------------------------------------------------------------
class RoughClip(Strict):
    shot_id: int
    beat_id: str
    in_s: float
    out_s: float
    purpose: str


class RoughCutResponse(Strict):
    clips: List[RoughClip]
    notes: str


# -- match-cut verification -----------------------------------------------
Verdict = Literal["match_cut", "acceptable_cut", "jump_cut", "bad_cut"]
MatchType = Literal["graphic", "motion", "action", "none"]


class PairVerdict(Strict):
    pair_id: str
    verdict: Verdict
    match_type: MatchType
    confidence: float
    note: str


class VerifyResponse(Strict):
    pairs: List[PairVerdict]


# -- polish ------------------------------------------------------------------
EditOp = Literal["trim_head", "trim_tail", "extend_head", "extend_tail", "drop", "move_before", "set_transition", "keep"]


class PolishEdit(Strict):
    op: EditOp
    clip_id: str
    seconds: float = Field(description="Amount for trim/extend ops; 0 otherwise.")
    target_clip_id: str = Field(description="For move_before: the clip to move in front of; '' otherwise.")
    transition: TransitionKind
    reason: str


class PolishResponse(Strict):
    assessment: str
    edits: List[PolishEdit]
