"""AI editing pipeline: sample footage, find the narrative, rough cut, then
iterative match-cut / continuity / pacing / transition / polish passes.

Usage::

    python -m matchcut.pipeline run footage.mp4 --brief "..." --target-duration 60
"""
from .config import PipelineConfig
from .pipeline import Pipeline
from .timeline import Clip, Timeline, Transition

__all__ = ["Pipeline", "PipelineConfig", "Timeline", "Clip", "Transition"]
