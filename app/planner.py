"""Pipeline orchestration: interpret -> guardrail -> optimize -> validate.

Damage-control ladder (never returns an invalid response if one is achievable):
  1. LP with all interpreted directives
  2. if infeasible -> LP without directives (still 100% GridWise-valid)
  3. if still infeasible -> battery-idle baseline (grid covers demand)
Each response is self-validated with the judge-replay validator before return.
"""
from __future__ import annotations

import logging
import time
from typing import List

from .config import settings
from .guardrails import (
    EffectiveConstraints,
    build_constraints,
    effective_min_energy,
    effective_solar,
)
from .interpreter import interpret_notes
from .optimizer import solve_schedule
from .postprocess import (
    build_hourly_plan,
    build_plan_summary,
    compute_totals,
    finalize_energy,
)
from .schemas import ScenarioRequest
from .validator import judge_replay

logger = logging.getLogger("gridwise.planner")


def _request_dicts(req: ScenarioRequest) -> dict:
    return {
        "scenario_id": req.scenario_id,
        "operator_notes": req.operator_notes,
        "hours": [h.model_dump() for h in req.hours],
        "battery": req.battery.model_dump(),
    }


def _solve_and_build(
    hours: List[dict],
    battery: dict,
    ec: EffectiveConstraints,
    entries: List[dict],
    scenario_id: str,
    n_notes: int,
) -> dict | None:
    result = solve_schedule(hours, battery, ec)
    if result.status != "optimal":
        return None

    eff_solar = effective_solar([h["solar_kwh"] for h in hours], ec)
    plan, _meta = build_hourly_plan(result, hours, eff_solar)
    finalize_energy(plan, battery["initial_energy_kwh"])
    tariff = [h["tariff_bdt_per_kwh"] for h in hours]
    totals = compute_totals(plan, tariff)

    n_applied = sum(1 for e in entries if e["directive_type"] != "no_op")
    reserve_hours = len(ec.reserve_floor)
    capped_hours = len(ec.grid_cap)
    summary = build_plan_summary(n_notes, n_applied, totals, reserve_hours, capped_hours)

    return {
        "scenario_id": scenario_id,
        "directive_interpretation": entries,
        "hourly_plan": plan,
        "total_grid_kwh": totals["total_grid_kwh"],
        "total_cost_bdt": totals["total_cost_bdt"],
        "peak_grid_kwh": totals["peak_grid_kwh"],
        "plan_summary": summary,
    }


def _baseline_plan(hours: List[dict], battery: dict, entries: List[dict], scenario_id: str, n_notes: int) -> dict:
    """Battery-idle feasible fallback: solar covers what it can, grid the rest."""
    e0 = battery["initial_energy_kwh"]
    plan = []
    for h in range(24):
        demand = hours[h]["demand_kwh"]
        solar = hours[h]["solar_kwh"]
        solar_used = min(demand, solar)
        plan.append({
            "hour": h,
            "grid_kwh": round(demand - solar_used, 6),
            "solar_used_kwh": round(solar_used, 6),
            "battery_action": "idle",
            "battery_kwh": 0.0,
            "battery_energy_after_kwh": round(e0, 6),
        })
    tariff = [h["tariff_bdt_per_kwh"] for h in hours]
    totals = compute_totals(plan, tariff)
    n_applied = sum(1 for e in entries if e["directive_type"] != "no_op")
    summary = build_plan_summary(n_notes, n_applied, totals)
    return {
        "scenario_id": scenario_id,
        "directive_interpretation": entries,
        "hourly_plan": plan,
        "total_grid_kwh": totals["total_grid_kwh"],
        "total_cost_bdt": totals["total_cost_bdt"],
        "peak_grid_kwh": totals["peak_grid_kwh"],
        "plan_summary": summary,
    }


def optimize_scenario(req: ScenarioRequest) -> dict:
    """Full pipeline for one scenario. Raises ValueError on semantic dead ends."""
    t0 = time.time()
    deadline = t0 + settings.request_budget_s

    request = _request_dicts(req)
    hours = sorted(request["hours"], key=lambda h: h["hour"])
    battery = request["battery"]
    notes = request["operator_notes"]
    n_notes = len(notes)

    # 1) LLM interpretation (cache -> primary -> retry -> fallback model -> emergency)
    entries, source = interpret_notes(notes, battery["capacity_kwh"], deadline)
    logger.info("scenario=%s interpretation_source=%s elapsed=%.2fs",
                req.scenario_id, source, time.time() - t0)

    # 2) deterministic constraints from the validated interpretation
    ec = build_constraints(entries, hours, battery)

    # 3) optimize (with damage-control ladder)
    response = _solve_and_build(hours, battery, ec, entries, req.scenario_id, n_notes)
    if response is None:
        logger.warning("scenario=%s LP infeasible with directives — relaxing", req.scenario_id)
        response = _solve_and_build(
            hours, battery, EffectiveConstraints(), entries, req.scenario_id, n_notes,
        )
    if response is None:
        logger.warning("scenario=%s LP infeasible without directives — baseline plan", req.scenario_id)
        response = _baseline_plan(hours, battery, entries, req.scenario_id, n_notes)

    # 4) self-validation (judge replay) before returning
    violations = judge_replay(response, request, entries)
    if violations:
        logger.warning("scenario=%s self-validation found %d violation(s): %s",
                       req.scenario_id, len(violations), violations[:5])
        # last-chance repair: re-solve without directives, keep interpretation
        relaxed = _solve_and_build(
            hours, battery, EffectiveConstraints(), entries, req.scenario_id, n_notes,
        )
        if relaxed is not None:
            relaxed_violations = judge_replay(relaxed, request, entries)
            base_violations = [v for v in relaxed_violations if "window" not in v and "cap" not in v and "minimum" not in v]
            if len(base_violations) < len([v for v in violations if "window" not in v and "cap" not in v and "minimum" not in v]):
                response = relaxed

    logger.info("scenario=%s completed in %.2fs cost=%.2f",
                req.scenario_id, time.time() - t0, response["total_cost_bdt"])
    return response
