"""Model backends.

``LLMBackend.structured`` takes a request (system prompt, text, images, payload
and a pydantic schema) and returns a validated instance of that schema.

* ``AnthropicBackend`` calls the Claude API with structured outputs.
* ``MockBackend`` answers deterministically from the request payload. It lets
  the full pipeline run offline (tests, dry runs, CI) and documents the exact
  contract every stage relies on.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Type, TypeVar

from PIL import Image
from pydantic import BaseModel, ValidationError

from .config import LLMConfig
from .events import ModelCall
from .sampling import encode_jpeg_b64
from . import schemas

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMRequest:
    name: str  # describe | narrative | rough_cut | verify_pairs | polish
    system: str
    text: str
    schema: Type[BaseModel]
    images: List[Image.Image] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)  # machine data (mock backend uses it)


class CallLog:
    """Append-only JSONL log of every model call (prompts, usage, timing)."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: Dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")


class LLMBackend:
    def __init__(self, cfg: LLMConfig, log: Optional[CallLog] = None):
        self.cfg = cfg
        self.log = log or CallLog(None)
        self.calls = 0
        self.usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.on_call: Optional[Callable[[ModelCall], None]] = None  # set by the pipeline to emit events

    def structured(self, request: LLMRequest) -> BaseModel:
        raise NotImplementedError

    def _record_call(self, name: str, images: int, seconds: float, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.usage["input_tokens"] += int(input_tokens)
        self.usage["output_tokens"] += int(output_tokens)
        if self.on_call is not None:
            self.on_call(
                ModelCall(
                    name=name,
                    images=images,
                    seconds=round(seconds, 3),
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
                    model=self.cfg.model,
                )
            )


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------
class AnthropicBackend(LLMBackend):
    def __init__(self, cfg: LLMConfig, log: Optional[CallLog] = None):
        super().__init__(cfg, log)
        try:
            import anthropic  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise LLMError("pip install anthropic to use the anthropic backend") from e
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=cfg.timeout_seconds, max_retries=cfg.max_retries)

    def _content(self, request: LLMRequest) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        for img in request.images:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": encode_jpeg_b64(img),
                    },
                }
            )
        blocks.append({"type": "text", "text": request.text})
        return blocks

    def structured(self, request: LLMRequest) -> BaseModel:
        schema = request.schema.model_json_schema()
        kwargs: Dict[str, Any] = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=[{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": self._content(request)}],
            output_config={
                "effort": self.cfg.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        if self.cfg.thinking_display != "omitted":
            kwargs["thinking"] = {"type": "adaptive", "display": self.cfg.thinking_display}

        started = time.time()
        last_error: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries + 1):
            response = self._call(kwargs)
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise LLMError(f"model declined request {request.name!r}: {details}")
            text = next((b.text for b in response.content if b.type == "text"), "")
            usage = getattr(response, "usage", None)
            in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
            out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0
            try:
                parsed = request.schema.model_validate_json(text)
                self._record_call(request.name, len(request.images), time.time() - started, in_tok, out_tok)
                self.log.write(
                    {
                        "name": request.name,
                        "model": self.cfg.model,
                        "images": len(request.images),
                        "seconds": round(time.time() - started, 2),
                        "stop_reason": response.stop_reason,
                        "usage": usage.to_dict() if usage is not None and hasattr(usage, "to_dict") else None,
                        "prompt": request.text,
                        "response": text,
                    }
                )
                return parsed
            except (ValidationError, ValueError) as e:
                last_error = e
                if attempt < self.cfg.max_retries:
                    continue
        raise LLMError(f"could not parse model output for {request.name!r}: {last_error}")

    def _call(self, kwargs: Dict[str, Any]):
        anthropic = self._anthropic
        try:
            if self.cfg.refusal_fallbacks:
                with self.client.beta.messages.stream(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **kwargs,
                ) as stream:
                    return stream.get_final_message()
            with self.client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed (set ANTHROPIC_API_KEY or run `ant auth login`)") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"bad request to the Claude API: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited by the Claude API after retries") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("could not reach the Claude API") from e


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------
class MockBackend(LLMBackend):
    """Deterministic stand-in that reasons only from the machine payload."""

    def structured(self, request: LLMRequest) -> BaseModel:
        handler = getattr(self, f"_{request.name}", None)
        if handler is None:
            raise LLMError(f"mock backend has no handler for {request.name!r}")
        result = handler(request.payload)
        self._record_call(request.name, len(request.images), 0.0, 0, 0)
        self.log.write({"name": request.name, "mock": True, "images": len(request.images), "prompt": request.text})
        return request.schema.model_validate(result)

    @staticmethod
    def _describe(p: Dict[str, Any]) -> Dict[str, Any]:
        shots = []
        for s in p["shots"]:
            moving = s["motion_energy"] > 0.015
            shots.append(
                dict(
                    shot_id=s["shot_id"],
                    description=f"Shot {s['shot_id']}: {'moving' if moving else 'static'} frame, "
                    f"{'bright' if s['brightness'] > 0.5 else 'dark'} image, {s['duration']:.1f}s.",
                    subject="unlabelled subject",
                    setting="unknown",
                    shot_size="medium",
                    camera_movement="handheld" if moving else "static",
                    subject_motion=s.get("motion_direction", "none") if moving else "none",
                    dominant_shapes="unknown",
                    mood="neutral",
                    tags=["moving" if moving else "static"],
                    key_action="none",
                    quality=0.5 + 0.3 * min(1.0, s["motion_energy"] * 10),
                    usable=s["brightness"] > 0.02,
                )
            )
        return {"shots": shots}

    @staticmethod
    def _narrative(p: Dict[str, Any]) -> Dict[str, Any]:
        usable = [s for s in p["shots"] if s.get("usable", True)]
        target = p.get("target_duration") or min(sum(s["duration"] for s in usable) * 0.5, 90.0)
        n_beats = max(1, min(3, len(usable)))
        beats = []
        size = -(-len(usable) // n_beats)
        titles = ["Setup", "Development", "Resolution"]
        for b in range(n_beats):
            chunk = usable[b * size : (b + 1) * size]
            if not chunk:
                continue
            beats.append(
                dict(
                    id=f"b{b + 1}",
                    title=titles[b],
                    intent=f"{titles[b]} of the visual story.",
                    emotional_tone="neutral",
                    pace="medium",
                    target_duration_s=round(target / n_beats, 2),
                    shot_ids=[s["shot_id"] for s in chunk],
                    transition_in="cut" if b == 0 else "dissolve",
                )
            )
        return dict(
            logline="A chronological pass through the footage.",
            theme="observation",
            structure_notes="Mock backend: shots are grouped chronologically into beats.",
            beats=beats,
            unused_shot_ids=[s["shot_id"] for s in p["shots"] if not s.get("usable", True)],
            rationale="mock",
        )

    @staticmethod
    def _rough_cut(p: Dict[str, Any]) -> Dict[str, Any]:
        shots = {s["shot_id"]: s for s in p["shots"]}
        clips = []
        for beat in p["beats"]:
            budget = float(beat["target_duration_s"])
            for sid in beat["shot_ids"]:
                if budget <= p["min_clip"] * 0.5:
                    break
                s = shots[sid]
                length = min(p["max_clip"], s["end"] - s["start"], max(p["min_clip"], budget))
                if length < p["min_clip"]:
                    continue
                centre = s.get("interest_peak", (s["start"] + s["end"]) / 2)
                in_s = max(s["start"], min(centre - length / 2, s["end"] - length))
                clips.append(
                    dict(shot_id=sid, beat_id=beat["id"], in_s=round(in_s, 3), out_s=round(in_s + length, 3), purpose="mock")
                )
                budget -= length
        return {"clips": clips, "notes": "mock rough cut"}

    @staticmethod
    def _verify_pairs(p: Dict[str, Any]) -> Dict[str, Any]:
        out = []
        for pair in p["pairs"]:
            score = pair["combined"]
            if pair.get("jump_cut_risk"):
                verdict, mtype = "jump_cut", "none"
            elif score >= p.get("threshold", 0.55):
                verdict = "match_cut"
                mtype = "motion" if pair.get("motion_type") == "moving" else "graphic"
            else:
                verdict, mtype = "acceptable_cut", "none"
            out.append(dict(pair_id=pair["pair_id"], verdict=verdict, match_type=mtype, confidence=round(score, 3), note="mock"))
        return {"pairs": out}

    @staticmethod
    def _polish(p: Dict[str, Any]) -> Dict[str, Any]:
        return {"assessment": "mock: cut left unchanged", "edits": []}


def make_backend(cfg: LLMConfig, log_path: Optional[Path] = None) -> LLMBackend:
    log = CallLog(log_path)
    if cfg.backend == "mock":
        return MockBackend(cfg, log)
    if cfg.backend == "anthropic":
        return AnthropicBackend(cfg, log)
    raise LLMError(f"unknown llm backend {cfg.backend!r}")
