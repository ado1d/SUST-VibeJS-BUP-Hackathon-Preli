"""Generate tests/my_sample_cases/gridwise_my_cases.json with fresh cases."""
from __future__ import annotations

import json
import os
from typing import Any

DAY_PROFILE: list[dict[str, Any]] = [
    {"hour": 0,  "demand_kwh": 90,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 1,  "demand_kwh": 85,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 2,  "demand_kwh": 80,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 3,  "demand_kwh": 80,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 4,  "demand_kwh": 85,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 5,  "demand_kwh": 95,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 6,  "demand_kwh": 110, "solar_kwh": 5,   "tariff_bdt_per_kwh": 8},
    {"hour": 7,  "demand_kwh": 130, "solar_kwh": 20,  "tariff_bdt_per_kwh": 10},
    {"hour": 8,  "demand_kwh": 150, "solar_kwh": 50,  "tariff_bdt_per_kwh": 12},
    {"hour": 9,  "demand_kwh": 165, "solar_kwh": 90,  "tariff_bdt_per_kwh": 14},
    {"hour": 10, "demand_kwh": 175, "solar_kwh": 130, "tariff_bdt_per_kwh": 16},
    {"hour": 11, "demand_kwh": 180, "solar_kwh": 160, "tariff_bdt_per_kwh": 16},
    {"hour": 12, "demand_kwh": 185, "solar_kwh": 180, "tariff_bdt_per_kwh": 15},
    {"hour": 13, "demand_kwh": 180, "solar_kwh": 170, "tariff_bdt_per_kwh": 14},
    {"hour": 14, "demand_kwh": 170, "solar_kwh": 140, "tariff_bdt_per_kwh": 13},
    {"hour": 15, "demand_kwh": 165, "solar_kwh": 90,  "tariff_bdt_per_kwh": 14},
    {"hour": 16, "demand_kwh": 170, "solar_kwh": 45,  "tariff_bdt_per_kwh": 18},
    {"hour": 17, "demand_kwh": 185, "solar_kwh": 10,  "tariff_bdt_per_kwh": 22},
    {"hour": 18, "demand_kwh": 205, "solar_kwh": 0,   "tariff_bdt_per_kwh": 28},
    {"hour": 19, "demand_kwh": 215, "solar_kwh": 0,   "tariff_bdt_per_kwh": 30},
    {"hour": 20, "demand_kwh": 205, "solar_kwh": 0,   "tariff_bdt_per_kwh": 26},
    {"hour": 21, "demand_kwh": 175, "solar_kwh": 0,   "tariff_bdt_per_kwh": 18},
    {"hour": 22, "demand_kwh": 135, "solar_kwh": 0,   "tariff_bdt_per_kwh": 10},
    {"hour": 23, "demand_kwh": 105, "solar_kwh": 0,   "tariff_bdt_per_kwh": 7},
]


def make_case(case_id, label, operator_notes, battery, rationale):
    return {
        "id": case_id,
        "label": label,
        "input": {
            "scenario_id": case_id,
            "operator_notes": operator_notes,
            "hours": DAY_PROFILE,
            "battery": battery,
        },
        "rationale": rationale,
    }


CASES = [
    make_case(
        "MY-01",
        "Cloudy noon + blackout discharge window",
        [
            "Heavy monsoon clouds from 11:00 to 14:00 are expected to cut usable PV output to about 30 percent of the forecast during that block.",
            "Substation maintenance: battery must not push energy back out between 7 PM and 9 PM this evening.",
        ],
        battery={"capacity_kwh": 200, "initial_energy_kwh": 100, "minimum_energy_kwh": 30,
                 "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50},
        rationale="solar_reduction (11-13, factor 0.3) + no_discharge_window (18, 19)",
    ),
    make_case(
        "MY-02",
        "Fire-pump reserve in kWh",
        ["Keep at least 75 kWh sitting in the battery storage between 17:00 and 21:00 for the fire-pump contingency plan."],
        battery={"capacity_kwh": 180, "initial_energy_kwh": 90, "minimum_energy_kwh": 20,
                 "max_charge_kwh_per_hour": 45, "max_discharge_kwh_per_hour": 45},
        rationale="absolute kWh minimum_battery_reserve",
    ),
    make_case(
        "MY-03",
        "All notes unrelated to today's schedule",
        ["Reminder: cafeteria menu changes on Friday.",
         "IT will roll out a new badge system next month.",
         "Parking lot B will be repainted over the weekend."],
        battery={"capacity_kwh": 150, "initial_energy_kwh": 60, "minimum_energy_kwh": 15,
                 "max_charge_kwh_per_hour": 35, "max_discharge_kwh_per_hour": 35},
        rationale="three unrelated notes -> all no_op",
    ),
    make_case(
        "MY-04",
        "Dust storm + 60% evening reserve",
        ["A dust storm is forecast between 13:00 and 16:00. Treat usable solar as 40 percent of forecast for those hours.",
         "Hold at least 60 percent of the battery capacity in reserve from 18:00 to 21:00 to cover an upcoming community event."],
        battery={"capacity_kwh": 250, "initial_energy_kwh": 130, "minimum_energy_kwh": 40,
                 "max_charge_kwh_per_hour": 60, "max_discharge_kwh_per_hour": 60},
        rationale="solar_reduction + percent-of-capacity minimum_battery_reserve",
    ),
    make_case(
        "MY-05",
        "Late-night inverter lockout + import ceiling",
        ["Inverter firmware reboot is scheduled between 23:00 and 02:00. The battery cannot discharge during that window.",
         "The utility has imposed a 120 kWh-per-hour import ceiling from 18:00 to 22:00 due to feeder constraints."],
        battery={"capacity_kwh": 180, "initial_energy_kwh": 70, "minimum_energy_kwh": 20,
                 "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50},
        rationale="cross-midnight no_discharge_window + max_grid_window",
    ),
    make_case(
        "MY-06",
        "Cheap-tariff charging disabled",
        ["Solar inverter inspections block battery charging between midnight and 04:00.",
         "Heads-up: the new cafeteria will open next quarter."],
        battery={"capacity_kwh": 220, "initial_energy_kwh": 80, "minimum_energy_kwh": 25,
                 "max_charge_kwh_per_hour": 60, "max_discharge_kwh_per_hour": 60},
        rationale="no_charge_window over cheap-tariff hours + distractor",
    ),
]


def main():
    out_path = os.path.join(os.path.dirname(__file__), "gridwise_my_cases.json")
    payload = {
        "_meta": {"title": "GridWise fresh test cases", "case_count": len(CASES),
                  "note": "Inputs only. POST each case.input to /optimize-energy."},
        "cases": CASES,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {out_path} ({len(CASES)} cases)")


if __name__ == "__main__":
    main()
