"""Self-validation: replays the final response exactly like the judge (Section 11).

This module is the local twin of the hidden judge harness. It re-checks, on every
response before it is returned:
  - interpretation structure (one entry per note, order, applies semantics)
  - hourly_plan structure (24 unique hours 0..23, finite non-negative values)
  - energy balance every hour
  - effective-solar usage (after solar_reduction composition)
  - battery transitions, bounds, rate limits, action consistency
  - directive application: reserve floors, no-charge, no-discharge, grid caps
  - end-of-day battery neutrality
  - reported totals match recalculation from hourly_plan

Every hidden-case failure mode listed in the Participant Guide's penalty table
is detectable here first — which is what lets the service self-correct instead
of shipping an invalid schedule.
"""
from __future__ import annotations

import math
from typing import Dict, List

from .schemas import BATTERY_ACTIONS, DIRECTIVE_TYPES

TOL = 0.01


def _close(a: float, b: float, tol: float = TOL) -> bool:
    return abs(a - b) <= tol


def judge_replay(
    response: dict,
    request: dict,
    interpretation: List[dict] | None = None,
) -> List[str]:
    """Return a list of violation strings. Empty list == the case would pass."""
    v: List[str] = []
    tol = TOL

    notes = request.get("operator_notes", [])
    n_notes = len(notes)
    hours = {h["hour"]: h for h in request.get("hours", [])}
    batt = request.get("battery", {})

    interp = interpretation if interpretation is not None else response.get("directive_interpretation", [])

    # ------------------------------------------------ interpretation structure
    if len(interp) != n_notes:
        v.append(f"directive_interpretation has {len(interp)} entries for {n_notes} notes")
    for i, e in enumerate(interp):
        if e.get("note_index") != i:
            v.append(f"interpretation[{i}].note_index != {i}")
        dt = e.get("directive_type")
        if dt not in DIRECTIVE_TYPES:
            v.append(f"interpretation[{i}].directive_type {dt!r} unsupported")
            continue
        adj = e.get("structured_adjustment")
        if dt == "no_op":
            if e.get("applies") is not False:
                v.append(f"interpretation[{i}] no_op must have applies=false")
            if adj is not None:
                v.append(f"interpretation[{i}] no_op must have structured_adjustment=null")
        else:
            if e.get("applies") is not True:
                v.append(f"interpretation[{i}] {dt} must have applies=true")
            if not isinstance(adj, dict):
                v.append(f"interpretation[{i}] {dt} missing structured_adjustment")
                continue
            hs = adj.get("hours", [])
            if hs != sorted(set(hs)) or any(not (0 <= h <= 23) for h in hs):
                v.append(f"interpretation[{i}] hours must be unique ascending 0..23")
            if dt == "solar_reduction":
                f = adj.get("factor")
                if not (isinstance(f, (int, float)) and 0.0 <= f <= 1.0):
                    v.append(f"interpretation[{i}] solar factor out of [0,1]")
            if dt == "minimum_battery_reserve":
                m = adj.get("minimum_energy_kwh")
                if not (isinstance(m, (int, float)) and 0 <= m <= batt.get("capacity_kwh", math.inf) + tol):
                    v.append(f"interpretation[{i}] reserve value invalid")
            if dt == "max_grid_window":
                c = adj.get("max_grid_kwh")
                if not (isinstance(c, (int, float)) and c >= 0):
                    v.append(f"interpretation[{i}] max_grid_kwh invalid")

    # --------------------------------------------------- compose directives
    solar_factor: Dict[int, float] = {}
    reserve_floor: Dict[int, float] = {}
    no_charge: set = set()
    no_discharge: set = set()
    grid_cap: Dict[int, float] = {}
    for e in interp:
        dt = e.get("directive_type")
        adj = e.get("structured_adjustment") or {}
        hs = adj.get("hours", [])
        if dt == "solar_reduction":
            for h in hs:
                solar_factor[h] = min(solar_factor.get(h, 1.0), adj["factor"])
        elif dt == "minimum_battery_reserve":
            for h in hs:
                reserve_floor[h] = max(reserve_floor.get(h, -1.0), adj["minimum_energy_kwh"])
        elif dt == "no_charge_window":
            no_charge.update(hs)
        elif dt == "no_discharge_window":
            no_discharge.update(hs)
        elif dt == "max_grid_window":
            for h in hs:
                grid_cap[h] = min(grid_cap.get(h, math.inf), adj["max_grid_kwh"])

    eff_solar = {
        h: hours[h]["solar_kwh"] * solar_factor.get(h, 1.0) for h in range(24)
    }
    min_energy = {
        h: max(batt.get("minimum_energy_kwh", 0.0), reserve_floor.get(h, 0.0))
        for h in range(24)
    }

    # ------------------------------------------------------ hourly_plan checks
    plan = response.get("hourly_plan", [])
    if len(plan) != 24:
        v.append(f"hourly_plan has {len(plan)} entries, expected 24")
        return v
    plan_hours = [r.get("hour") for r in plan]
    if sorted(plan_hours) != list(range(24)):
        v.append("hourly_plan must contain exactly hours 0..23")
        return v
    plan_by_hour = {r["hour"]: r for r in plan}

    for h in range(24):
        r = plan_by_hour[h]
        for k in ("grid_kwh", "solar_used_kwh", "battery_kwh", "battery_energy_after_kwh"):
            val = r.get(k)
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val < -tol:
                v.append(f"hour {h}: {k} must be finite and non-negative (got {val!r})")
        act = r.get("battery_action")
        if act not in BATTERY_ACTIONS:
            v.append(f"hour {h}: battery_action {act!r} invalid")
        if act == "idle" and abs(r.get("battery_kwh", 0.0)) > tol:
            v.append(f"hour {h}: idle action must have battery_kwh = 0")
        if act in ("charge", "discharge") and r.get("battery_kwh", 0.0) <= 0 and act != "idle":
            if r.get("battery_kwh", 0.0) <= tol and r.get("battery_kwh", 0.0) >= 0:
                # zero-magnitude charge/discharge is normalized to idle
                v.append(f"hour {h}: {act} with battery_kwh=0 should be idle")

    # battery transitions / bounds / rate limits
    e_prev = batt.get("initial_energy_kwh", 0.0)
    for h in range(24):
        r = plan_by_hour[h]
        act, amt = r["battery_action"], r.get("battery_kwh", 0.0)
        if act == "charge":
            if amt > batt.get("max_charge_kwh_per_hour", math.inf) + tol:
                v.append(f"hour {h}: charge {amt} exceeds rate limit")
            if h in no_charge:
                v.append(f"hour {h}: charging during no_charge_window")
            e_expect = e_prev + amt
        elif act == "discharge":
            if amt > batt.get("max_discharge_kwh_per_hour", math.inf) + tol:
                v.append(f"hour {h}: discharge {amt} exceeds rate limit")
            if h in no_discharge:
                v.append(f"hour {h}: discharging during no_discharge_window")
            e_expect = e_prev - amt
        else:
            e_expect = e_prev
        e_after = r.get("battery_energy_after_kwh", 0.0)
        if not _close(e_after, e_expect, tol):
            v.append(f"hour {h}: battery transition {e_prev} -> {e_after} != expected {e_expect}")
        if e_after > batt.get("capacity_kwh", math.inf) + tol:
            v.append(f"hour {h}: battery energy {e_after} above capacity")
        if e_after < min_energy[h] - tol:
            v.append(f"hour {h}: battery energy {e_after} below active minimum {min_energy[h]}")
        e_prev = e_after

    # energy balance / solar / grid cap
    for h in range(24):
        r = plan_by_hour[h]
        d = hours[h]["demand_kwh"]
        charge = r["battery_kwh"] if r["battery_action"] == "charge" else 0.0
        discharge = r["battery_kwh"] if r["battery_action"] == "discharge" else 0.0
        lhs = r["grid_kwh"] + r["solar_used_kwh"] + discharge
        rhs = d + charge
        if not _close(lhs, rhs, tol):
            v.append(f"hour {h}: energy balance {lhs} != {rhs}")
        if r["solar_used_kwh"] > eff_solar[h] + tol:
            v.append(f"hour {h}: solar_used {r['solar_used_kwh']} exceeds effective solar {eff_solar[h]}")
        if h in grid_cap and r["grid_kwh"] > grid_cap[h] + tol:
            v.append(f"hour {h}: grid {r['grid_kwh']} exceeds cap {grid_cap[h]}")

    # end-of-day neutrality
    if not _close(plan_by_hour[23]["battery_energy_after_kwh"], batt.get("initial_energy_kwh", 0.0), tol):
        v.append("end-of-day battery energy does not return to initial level")

    # totals
    total_grid = sum(r["grid_kwh"] for r in plan)
    total_cost = sum(r["grid_kwh"] * hours[r["hour"]]["tariff_bdt_per_kwh"] for r in plan)
    peak = max(r["grid_kwh"] for r in plan)
    if not _close(response.get("total_grid_kwh", -1), total_grid, tol):
        v.append(f"total_grid_kwh {response.get('total_grid_kwh')} != recalc {round(total_grid, 6)}")
    if not _close(response.get("total_cost_bdt", -1), total_cost, tol):
        v.append(f"total_cost_bdt {response.get('total_cost_bdt')} != recalc {round(total_cost, 6)}")
    if not _close(response.get("peak_grid_kwh", -1), peak, tol):
        v.append(f"peak_grid_kwh {response.get('peak_grid_kwh')} != recalc {round(peak, 6)}")

    return v
