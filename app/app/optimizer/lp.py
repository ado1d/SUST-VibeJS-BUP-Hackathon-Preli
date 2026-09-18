"""Exact 24-hour energy schedule optimizer (linear program, SciPy HiGHS).

Decision variables (120 total, all continuous):
    g[h]  grid purchase            >= 0, <= grid cap (if max_grid_window)
    s[h]  solar used               0..effective_solar[h]
    c[h]  battery charge amount    0..max_charge (0 inside no_charge_window)
    d[h]  battery discharge amount 0..max_discharge (0 inside no_discharge_window)
    e[h]  battery energy after h   min_reserve[h]..capacity

Constraints:
    balance :  g[h] + s[h] + d[h] - c[h] = demand[h]            (24 rows)
    state   :  e[h] - e[h-1] - c[h] + d[h] = 0, e[-1] = E0      (24 rows)
    neutral :  e[23] = E0                                       (1 row)

Objective:  minimize  SUM tariff[h] * g[h]

Because the model is a linear program solved to proven optimality, the returned
cost equals the organizer optimal cost for the applied directives — which is
exactly what the Optimization Quality formula min(1, optimal/team) rewards.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from scipy.optimize import linprog

from ..guardrails import EffectiveConstraints

N = 24  # hours


@dataclass
class LpResult:
    status: str  # "optimal" | "infeasible" | "unbounded" | "error"
    grid: List[float] = field(default_factory=list)
    solar_used: List[float] = field(default_factory=list)
    charge: List[float] = field(default_factory=list)
    discharge: List[float] = field(default_factory=list)
    energy: List[float] = field(default_factory=list)
    objective: Optional[float] = None


def solve_schedule(
    hours: List[dict],
    battery: dict,
    ec: EffectiveConstraints,
) -> LpResult:
    """Solve the min-cost schedule LP. hours must be ordered 0..23."""
    demand = np.array([hours[h]["demand_kwh"] for h in range(N)], dtype=float)
    solar = np.array([hours[h]["solar_kwh"] for h in range(N)], dtype=float)
    tariff = np.array([hours[h]["tariff_bdt_per_kwh"] for h in range(N)], dtype=float)

    cap_kwh = float(battery["capacity_kwh"])
    e0 = float(battery["initial_energy_kwh"])
    base_min = float(battery["minimum_energy_kwh"])
    max_ch = float(battery["max_charge_kwh_per_hour"])
    max_dis = float(battery["max_discharge_kwh_per_hour"])

    # effective per-hour bounds after directives
    eff_solar = np.array([solar[h] * ec.solar_factor.get(h, 1.0) for h in range(N)])
    min_e = np.array([max(base_min, ec.reserve_floor.get(h, 0.0)) for h in range(N)])

    # ---- variable layout: g(0..23) s(24..47) c(48..71) d(72..95) e(96..119)
    IG, IS, IC, ID_, IE = 0, 24, 48, 72, 96
    n_vars = 5 * N

    cost = np.zeros(n_vars)
    cost[IG:IG + N] = tariff

    # equality system
    A_eq = np.zeros((2 * N + 1, n_vars))
    b_eq = np.zeros(2 * N + 1)

    for h in range(N):
        # energy balance: g + s + d - c = demand
        A_eq[h, IG + h] = 1.0
        A_eq[h, IS + h] = 1.0
        A_eq[h, ID_ + h] = 1.0
        A_eq[h, IC + h] = -1.0
        b_eq[h] = demand[h]
        # battery state: e[h] - e[h-1] - c[h] + d[h] = 0  (h=0: e[0] - c[0] + d[0] = E0)
        r = N + h
        A_eq[r, IE + h] = 1.0
        if h == 0:
            b_eq[r] = e0
        else:
            A_eq[r, IE + h - 1] = -1.0
        A_eq[r, IC + h] = -1.0
        A_eq[r, ID_ + h] = 1.0
    # end-of-day neutrality: e[23] = E0
    A_eq[2 * N, IE + N - 1] = 1.0
    b_eq[2 * N] = e0

    bounds = [(0.0, None)] * n_vars
    for h in range(N):
        gcap = ec.grid_cap.get(h)
        bounds[IG + h] = (0.0, gcap if gcap is not None else None)
        bounds[IS + h] = (0.0, float(eff_solar[h]))
        bounds[IC + h] = (0.0, 0.0 if h in ec.no_charge else max_ch)
        bounds[ID_ + h] = (0.0, 0.0 if h in ec.no_discharge else max_dis)
        bounds[IE + h] = (float(min_e[h]), cap_kwh)

    try:
        res = linprog(
            c=cost,
            A_ub=None,
            b_ub=None,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
        )
    except Exception:
        return LpResult(status="error")

    if res.status == 2:
        return LpResult(status="infeasible")
    if res.status == 3:
        return LpResult(status="unbounded")
    if res.status != 0 or res.x is None:
        return LpResult(status="error")

    x = res.x
    return LpResult(
        status="optimal",
        grid=[float(x[IG + h]) for h in range(N)],
        solar_used=[float(x[IS + h]) for h in range(N)],
        charge=[float(x[IC + h]) for h in range(N)],
        discharge=[float(x[ID_ + h]) for h in range(N)],
        energy=[float(x[IE + h]) for h in range(N)],
        objective=float(res.fun),
    )
