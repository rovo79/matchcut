"""Temporal editorial review: play a junction of a stage timeline in mpv.

Sits between the engine (``matchcut.pipeline``) and the TUI (``matchcut.tui``): it never
imports Textual or the TUI, and the pipeline never imports it.
"""
