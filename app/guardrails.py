"""Deterministic guardrails for LLM-produced directive interpretations.

LLM output is treated as UNTRUSTED structured data until it passes every check here.
The Problem Statement (Section 08) is canonical for these rules:

  - directive_type must be one of the six supported values
  - note mapping: exactly one entry per note, note_index order 0..N-1
  - hours: unique integers 0..23, ascending
  - solar_reduction: factor in [0, 1] (usable fraction that REMAINS)
  - minimum_battery_reserve: 0 <= minimum_energy_kwh <= capacity
  - max_grid_window: max_grid_kwh finite and >= 0
  - applies semantics: no_op <=> applies=false <=> adjustment null;
    every other directive => applies=true with the exact required shape

Safe repairs (sorting/dedup of hours, applies coercion, missing explanation) are
applied automatically. Anything ambiguous is REJECTED so the caller can re-ask
the LLM or fall back safely — the service never invents constraints.
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from .schemas import DIRECTIVE_TYPES

HOURS_ONLY_TYPES = {"no_charge_window", "no_discharge_window"}


class GuardrailError(ValueError):
    """Raised when raw interpretation data cannot be safely normalized."""


def _clean_hours(raw) -> List[int]:
    """Validate + normalize an hours array: ints 0..23, unique, ascending."""
    if raw is None:
        raise GuardrailError("hours is missing")
    if not isinstance(raw, (list, tuple)):
        raise GuardrailError("hours must be an array")
    cleaned: List[int] = []
    for h in raw:
        if isinstance(h, bool) or not isinstance(h, (int, float)):
            raise GuardrailError(f"hour {h!r} is not a number")
        if isinstance(h, float):
            if not math.isfinite(h) or abs(h - round(h)) > 1e-9:
                raise GuardrailError(f"hour {h!r} is not a whole hour index")
            h = int(round(h))
        h = int(h)
        if not (0 <= h <= 23):
            raise GuardrailError(f"hour {h} out of range 0..23")
        cleaned.append(h)
    # dedupe + ascending order (safe, meaning-preserving normalization)
    cleaned = sorted(set(cleaned))
    if not cleaned:
        raise GuardrailError("hours array is empty")
    return cleaned


def _num(raw, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise GuardrailError(f"{name} must be a number")
    v = float(raw)
    if not math.isfinite(v):
        raise GuardrailError(f"{name} must be finite")
    return v


def normalize_entry(raw: dict, battery_capacity: float) -> dict:
    """Validate one raw interpretation entry and return the normalized entry.

    Raises GuardrailError with a human-readable reason on failure.
    """
    if not isinstance(raw, dict):
        raise GuardrailError("interpretation entry is not an object")

    note_index = raw.get("note_index")
    if isinstance(note_index, bool) or not isinstance(note_index, int):
        raise GuardrailError("note_index must be an integer")

    dtype = raw.get("directive_type")
    if dtype not in DIRECTIVE_TYPES:
        raise GuardrailError(f"unsupported directive_type {dtype!r}")

    adj = raw.get("structured_adjustment")
    applies = raw.get("applies")

    if dtype == "no_op":
        # no_op is the ONLY directive allowed with applies=false and null adjustment
        applies = False
        adj = None
    else:
        applies = True
        if not isinstance(adj, dict):
            raise GuardrailError(f"{dtype} requires a structured_adjustment object")
        hours = _clean_hours(adj.get("hours"))
        if dtype == "solar_reduction":
            factor = _num(adj.get("factor"), "factor")
            if not (0.0 <= factor <= 1.0):
                raise GuardrailError("solar_reduction factor must be within [0, 1]")
            adj = {"hours": hours, "factor": factor}
        elif dtype == "minimum_battery_reserve":
            mev = _num(adj.get("minimum_energy_kwh"), "minimum_energy_kwh")
            if mev < 0:
                raise GuardrailError("minimum_energy_kwh must be non-negative")
            if mev > battery_capacity + 1e-9:
                raise GuardrailError("minimum_energy_kwh exceeds battery capacity")
            adj = {"hours": hours, "minimum_energy_kwh": mev}
        elif dtype in HOURS_ONLY_TYPES:
            adj = {"hours": hours}
        elif dtype == "max_grid_window":
            cap = _num(adj.get("max_grid_kwh"), "max_grid_kwh")
            if cap < 0:
                raise GuardrailError("max_grid_kwh must be non-negative")
            adj = {"hours": hours, "max_grid_kwh": cap}

    explanation = raw.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        explanation = (
            "Operator note does not affect the 24-hour energy schedule."
            if dtype == "no_op"
            else f"Operator note interpreted as {dtype}."
        )

    return {
        "note_index": note_index,
        "applies": applies,
        "directive_type": dtype,
        "structured_adjustment": adj,
        "explanation": explanation,
    }


def normalize_interpretation(
    raw_entries,
    n_notes: int,
    battery_capacity: float,
) -> Tuple[List[dict], List[str]]:
    """Normalize a full LLM response body into ordered, validated entries.

    Returns (entries, errors). entries is empty when errors is non-empty.
    Every entry is guaranteed to satisfy the guardrail rules in Section 08.
    """
    errors: List[str] = []
    if not isinstance(raw_entries, list):
        return [], ["interpretation payload is not an array"]

    by_index: Dict[int, dict] = {}
    for pos, raw in enumerate(raw_entries):
        try:
            entry = normalize_entry(raw, battery_capacity)
        except GuardrailError as e:
            errors.append(f"entry[{pos}]: {e}")
            continue
        idx = entry["note_index"]
        if idx in by_index:
            errors.append(f"duplicate note_index {idx}")
            continue
        if not (0 <= idx < n_notes):
            errors.append(f"note_index {idx} out of range 0..{n_notes - 1}")
            continue
        by_index[idx] = entry

    missing = [i for i in range(n_notes) if i not in by_index]
    if missing:
        errors.append(f"missing interpretations for note_index {missing}")
    if errors:
        return [], errors

    return [by_index[i] for i in range(n_notes)], []


# ------------------------------------------------------------------ composition

class EffectiveConstraints:
    """Per-hour effective parameters after composing ALL directives.

    Composition is conservative so the plan satisfies every directive under the
    judge's independent replay:
      - multiple solar_reduction on the same hour -> strictest (minimum) factor
      - multiple reserve floors on the same hour  -> highest floor
      - multiple grid caps on the same hour       -> lowest cap
      - no_charge / no_discharge windows          -> union of hours
    """

    def __init__(self):
        self.solar_factor: Dict[int, float] = {}       # hour -> min factor
        self.reserve_floor: Dict[int, float] = {}      # hour -> max min_energy
        self.no_charge: set = set()
        self.no_discharge: set = set()
        self.grid_cap: Dict[int, float] = {}           # hour -> min cap

    def apply(self, entry: dict) -> None:
        adj = entry.get("structured_adjustment") or {}
        dtype = entry["directive_type"]
        if dtype == "no_op":
            return
        hours = adj.get("hours", [])
        if dtype == "solar_reduction":
            f = adj["factor"]
            for h in hours:
                self.solar_factor[h] = min(self.solar_factor.get(h, 1.0), f)
        elif dtype == "minimum_battery_reserve":
            m = adj["minimum_energy_kwh"]
            for h in hours:
                self.reserve_floor[h] = max(self.reserve_floor.get(h, -1.0), m)
        elif dtype == "no_charge_window":
            self.no_charge.update(hours)
        elif dtype == "no_discharge_window":
            self.no_discharge.update(hours)
        elif dtype == "max_grid_window":
            c = adj["max_grid_kwh"]
            for h in hours:
                self.grid_cap[h] = min(self.grid_cap.get(h, math.inf), c)


def build_constraints(entries: List[dict], hours: List[dict], battery: dict) -> EffectiveConstraints:
    ec = EffectiveConstraints()
    for e in entries:
        ec.apply(e)
    return ec


def effective_solar(solar: List[float], ec: EffectiveConstraints) -> List[float]:
    return [solar[h] * ec.solar_factor.get(h, 1.0) for h in range(24)]


def effective_min_energy(base_min: float, ec: EffectiveConstraints) -> List[float]:
    return [max(base_min, ec.reserve_floor.get(h, 0.0)) for h in range(24)]
