"""LLM interpretation path with a safe-failure ladder.

Ladder (fastest first):
  1. interpretation cache (repeated identical notes -> instant)
  2. primary OpenAI model, temperature 0, strict structured outputs
  3. one corrective re-ask with the guardrail error feedback
  4. fallback OpenAI model
  5. emergency rule-based classifier (deterministic, conservative)

The language model is the REQUIRED interpreter (Problem Statement Section 02);
the emergency classifier only runs when the LLM path fails, which is exactly
the SAFE FAILURE behavior demanded by Section 08 — the service must degrade
gracefully, never crash, and never invent unsupported directives.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import List, Optional, Tuple

from ..config import settings
from ..guardrails import normalize_interpretation
from .prompt import INTERPRETATION_JSON_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from . import emergency

logger = logging.getLogger("gridwise.interpreter")

_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from openai import OpenAI

                kwargs = {
                    "api_key": settings.openai_api_key,
                    "max_retries": 0,  # we control retries ourselves
                }
                if settings.openai_base_url:
                    kwargs["base_url"] = settings.openai_base_url
                _client = OpenAI(**kwargs)
    return _client


# ---------------------------------------------------------------- cache

_cache: "OrderedDict[tuple, List[dict]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(notes: List[str], battery_capacity: float) -> tuple:
    return (tuple(notes), round(float(battery_capacity), 6))


def _cache_get(key: tuple) -> Optional[List[dict]]:
    if not settings.cache_enabled:
        return None
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def _cache_put(key: tuple, value: List[dict]) -> None:
    if not settings.cache_enabled:
        return
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > settings.cache_max_entries:
            _cache.popitem(last=False)


# ---------------------------------------------------------------- LLM call

def _call_model(
    notes: List[str],
    battery_capacity: float,
    model: str,
    timeout_s: float,
    feedback: Optional[str] = None,
) -> Tuple[Optional[list], Optional[str]]:
    """One structured-output completion. Returns (raw_interpretations, error)."""
    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(notes, battery_capacity, feedback)},
            ],
            temperature=0.0,
            max_tokens=1200,
            timeout=timeout_s,
            response_format={
                "type": "json_schema",
                "json_schema": INTERPRETATION_JSON_SCHEMA,
            },
        )
        content = resp.choices[0].message.content
        data = json.loads(content)
        raw = data.get("interpretations")
        if not isinstance(raw, list):
            return None, "model returned no interpretations array"
        return raw, None
    except Exception as e:  # noqa: BLE001 — controlled failure, no secrets leaked
        logger.warning("LLM call failed (%s): %s", model, type(e).__name__)
        return None, f"model call error: {type(e).__name__}"


def _validate(raw: list, n_notes: int, capacity: float):
    return normalize_interpretation(raw, n_notes, capacity)


# ---------------------------------------------------------------- public API

def interpret_notes(
    notes: List[str],
    battery_capacity: float,
    deadline: Optional[float] = None,
) -> Tuple[List[dict], str]:
    """Interpret all operator notes. Returns (normalized_entries, source).

    source is one of: "cache" | "llm" | "llm-fallback" | "emergency".
    Every returned entry already passed the deterministic guardrails.
    """
    n = len(notes)
    key = _cache_key(notes, battery_capacity)

    hit = _cache_get(key)
    if hit is not None:
        return hit, "cache"

    def _time_left() -> float:
        return (deadline - time.time()) if deadline else float("inf")

    has_key = bool(settings.openai_api_key)

    # ---- primary model (+ one corrective retry with error feedback)
    if has_key and _time_left() > 2.0:
        raw, err = _call_model(notes, battery_capacity, settings.openai_model, settings.openai_timeout_s)
        if raw is not None:
            entries, errors = _validate(raw, n, battery_capacity)
            if not errors:
                _cache_put(key, entries)
                return entries, "llm"
            feedback = "; ".join(errors)
        else:
            feedback = err

        for _ in range(max(0, settings.llm_validation_retries)):
            if _time_left() < 3.0:
                break
            raw, err = _call_model(
                notes, battery_capacity, settings.openai_model,
                settings.openai_timeout_s, feedback=feedback,
            )
            if raw is not None:
                entries, errors = _validate(raw, n, battery_capacity)
                if not errors:
                    _cache_put(key, entries)
                    return entries, "llm"
                feedback = "; ".join(errors)
            else:
                feedback = err

    # ---- fallback model
    if has_key and _time_left() > 2.0:
        raw, err = _call_model(
            notes, battery_capacity, settings.openai_fallback_model,
            settings.openai_fallback_timeout_s, feedback=None,
        )
        if raw is not None:
            entries, errors = _validate(raw, n, battery_capacity)
            if not errors:
                _cache_put(key, entries)
                return entries, "llm-fallback"

    # ---- emergency deterministic classifier (SAFE FAILURE path)
    if not has_key:
        logger.warning("OPENAI_API_KEY not set — using emergency classifier "
                       "(NOT compliant for submission; set the key!)")
    else:
        logger.warning("LLM interpretation failed — using emergency classifier")
    entries = [emergency.classify(note, battery_capacity, i) for i, note in enumerate(notes)]
    entries, errors = _validate(entries, n, battery_capacity)
    if errors:
        # absolute last resort: everything is no_op (schedule stays GridWise-valid)
        entries = [
            {
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "Interpretation unavailable; treated as not affecting the schedule.",
            }
            for i in range(n)
        ]
    return entries, "emergency"
