"""Request/response schemas for the GridWise API contract.

The Problem Statement is canonical: field names, enums and shapes here mirror it exactly.
"""
from __future__ import annotations

import math
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

BATTERY_ACTIONS = {"charge", "discharge", "idle"}


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("value must be finite")
    return v


def _strict_number(v):
    """Accept JSON int/float only — reject strings and booleans."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("must be a JSON number")
    return v


# ---------------------------------------------------------------- request

class HourEntry(BaseModel):
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)

    _s = field_validator("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh", mode="before")(_strict_number)
    _f = field_validator("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh", mode="after")(_finite)
    _sh = field_validator("hour", mode="before")(_strict_number)


class Battery(BaseModel):
    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    _s = field_validator(
        "capacity_kwh", "initial_energy_kwh", "minimum_energy_kwh",
        "max_charge_kwh_per_hour", "max_discharge_kwh_per_hour", mode="before",
    )(_strict_number)
    _f = field_validator(
        "capacity_kwh", "initial_energy_kwh", "minimum_energy_kwh",
        "max_charge_kwh_per_hour", "max_discharge_kwh_per_hour", mode="after",
    )(_finite)

    @model_validator(mode="after")
    def _semantic_bounds(self):
        if self.initial_energy_kwh > self.capacity_kwh + 1e-9:
            raise ValueError("initial_energy_kwh exceeds capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh + 1e-9:
            raise ValueError("minimum_energy_kwh exceeds capacity_kwh")
        if self.initial_energy_kwh < self.minimum_energy_kwh - 1e-9:
            raise ValueError("initial_energy_kwh below minimum_energy_kwh")
        return self


class ScenarioRequest(BaseModel):
    scenario_id: str
    operator_notes: List[str] = Field(min_length=1, max_length=3)
    hours: List[HourEntry] = Field(min_length=24, max_length=24)
    battery: Battery

    @field_validator("operator_notes", mode="after")
    @classmethod
    def _notes_nonempty(cls, v: List[str]) -> List[str]:
        for i, note in enumerate(v):
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"operator_notes[{i}] must be a non-empty string")
        return v

    @field_validator("scenario_id", mode="after")
    @classmethod
    def _sid_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("scenario_id must be non-empty")
        return v

    @model_validator(mode="after")
    def _hours_cover_0_23(self):
        got = sorted(h.hour for h in self.hours)
        if got != list(range(24)):
            raise ValueError("hours must contain exactly the 24 unique hours 0..23")
        return self


# ---------------------------------------------------------------- response

class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: Optional[dict] = None
    explanation: str = ""


class HourlyPlanEntry(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: str
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
