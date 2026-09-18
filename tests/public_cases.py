"""Public sample case runner — the pre-submission gate.

Three modes:
  python tests/public_cases.py
      Optimizer + validity verification using the reference interpretations
      (no API key needed). Every case must be judge-replay-valid AND reach
      the reference optimal cost within 0.01 BDT.

  python tests/public_cases.py --llm
      Additionally runs the full LLM interpretation pipeline in-process
      (requires OPENAI_API_KEY) and compares against expected semantics.

  python tests/public_cases.py --base-url https://host
      Runs the full pipeline over HTTP against a deployed service.

Exit code 0 only when every case passes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.guardrails import (  # noqa: E402
    EffectiveConstraints,
    build_constraints,
    effective_solar,
)
from app.optimizer import solve_schedule  # noqa: E402
from app.postprocess import (  # noqa: E402
    build_hourly_plan,
    build_plan_summary,
    compute_totals,
    finalize_energy,
)
from app.validator import judge_replay  # noqa: E402

TOL = 0.01
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CASES = os.environ.get(
    "PUBLIC_CASES_JSON",
    os.path.join(HERE, "..", "public_cases", "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"),
)


def load_cases(path: str):
    with open(path) as f:
        return json.load(f)["cases"]


# ------------------------------------------------------------------ local solve

def solve_with_interpretation(case_input: dict, interpretation: list) -> dict:
    """Deterministic optimizer path using a GIVEN interpretation."""
    from app.schemas import ScenarioRequest  # local import to validate shape

    req = ScenarioRequest(**case_input)
    hours = sorted(case_input["hours"], key=lambda h: h["hour"])
    battery = case_input["battery"]
    ec = build_constraints(interpretation, hours, battery)

    result = solve_schedule(hours, battery, ec)
    if result.status != "optimal":
        raise RuntimeError(f"LP status: {result.status}")

    eff_solar = effective_solar([h["solar_kwh"] for h in hours], ec)
    plan, _ = build_hourly_plan(result, hours, eff_solar)
    finalize_energy(plan, battery["initial_energy_kwh"])
    totals = compute_totals(plan, [h["tariff_bdt_per_kwh"] for h in hours])
    n_applied = sum(1 for e in interpretation if e["directive_type"] != "no_op")
    summary = build_plan_summary(
        len(case_input["operator_notes"]), n_applied, totals,
        len(ec.reserve_floor), len(ec.grid_cap),
    )
    return {
        "scenario_id": case_input["scenario_id"],
        "directive_interpretation": interpretation,
        "hourly_plan": plan,
        **totals,
        "plan_summary": summary,
    }


# ------------------------------------------------------------------ comparisons

def check_interpretation(expected: list, got: list) -> list:
    problems = []
    if len(got) != len(expected):
        return [f"entry count {len(got)} != {len(expected)}"]
    for i, (e, g) in enumerate(zip(expected, got)):
        if g.get("note_index") != e["note_index"]:
            problems.append(f"[{i}] note_index mismatch")
        if g.get("directive_type") != e["directive_type"]:
            problems.append(f"[{i}] type {g.get('directive_type')} != {e['directive_type']}")
            continue
        if (g.get("applies") is True) != (e["applies"] is True):
            problems.append(f"[{i}] applies mismatch")
        ea, ga = e.get("structured_adjustment"), g.get("structured_adjustment")
        if ea is None and ga is None:
            continue
        if (ea is None) != (ga is None):
            problems.append(f"[{i}] adjustment null-ness mismatch")
            continue
        if sorted(ga.get("hours", [])) != sorted(ea.get("hours", [])):
            problems.append(f"[{i}] hours {ga.get('hours')} != {ea.get('hours')}")
        for k in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if k in ea or k in ga:
                ev, gv = ea.get(k), ga.get(k)
                if ev is None or gv is None:
                    if ev != gv:
                        problems.append(f"[{i}] {k} {gv!r} != {ev!r}")
                elif abs(float(gv) - float(ev)) > TOL:
                    problems.append(f"[{i}] {k} {gv} != {ev}")
    return problems


# ------------------------------------------------------------------ modes

def run_local(cases):
    print("=" * 78)
    print("MODE A — optimizer + validity (reference interpretation, no LLM key)")
    print("=" * 78)
    failures = 0
    for c in cases:
        cid = c["id"]
        expected_out = c["expected_output"]
        expected_interp = expected_out["directive_interpretation"]

        # sanity: our judge-replay validator accepts the organizers' reference output
        ref_violations = judge_replay(expected_out, c["input"], expected_interp)

        try:
            response = solve_with_interpretation(c["input"], expected_interp)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {cid}: solver error: {e}")
            failures += 1
            continue

        violations = judge_replay(response, c["input"], expected_interp)
        cost = response["total_cost_bdt"]
        ref_cost = expected_out["total_cost_bdt"]
        cost_ok = (cost <= ref_cost + TOL)
        status = "PASS" if (not violations and cost_ok and not ref_violations) else "FAIL"

        print(f"[{status}] {cid:10s} cost={cost:10.2f} ref={ref_cost:10.2f} "
              f"delta={cost - ref_cost:+8.4f} violations={len(violations)}")
        if ref_violations:
            print(f"       !! reference output itself flagged ({len(ref_violations)}): {ref_violations[:3]}")
        if violations:
            for v in violations[:6]:
                print(f"       - {v}")
        if not cost_ok:
            print(f"       - cost exceeds reference by more than {TOL}")
        if status == "FAIL":
            failures += 1
    print(f"\nMODE A: {len(cases) - failures}/{len(cases)} passed")
    return failures


def run_llm(cases):
    print("=" * 78)
    print("MODE B — full pipeline incl. LLM interpretation (in-process)")
    print("=" * 78)
    if not os.environ.get("OPENAI_API_KEY"):
        print("[SKIP] OPENAI_API_KEY not set")
        return 0
    from app.planner import optimize_scenario
    from app.schemas import ScenarioRequest

    failures = 0
    for c in cases:
        cid = c["id"]
        t0 = time.time()
        try:
            req = ScenarioRequest(**c["input"])
            response = optimize_scenario(req)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {cid}: pipeline error: {e}")
            failures += 1
            continue
        elapsed = time.time() - t0

        interp_problems = check_interpretation(
            c["expected_output"]["directive_interpretation"],
            response["directive_interpretation"],
        )
        violations = judge_replay(response, c["input"], response["directive_interpretation"])
        cost = response["total_cost_bdt"]
        ref_cost = c["expected_output"]["total_cost_bdt"]
        ok = not interp_problems and not violations and cost <= ref_cost + TOL
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {cid:10s} cost={cost:10.2f} ref={ref_cost:10.2f} "
              f"interp={'OK' if not interp_problems else 'MISMATCH'} "
              f"valid={'OK' if not violations else 'INVALID'} {elapsed:.2f}s")
        for p in interp_problems[:6]:
            print(f"       interp: {p}")
        for v in violations[:6]:
            print(f"       valid : {v}")
        if not ok:
            failures += 1
    print(f"\nMODE B: {len(cases) - failures}/{len(cases)} passed")
    return failures


def run_http(cases, base_url: str):
    print("=" * 78)
    print(f"MODE C — full pipeline over HTTP against {base_url}")
    print("=" * 78)
    import httpx

    failures = 0
    with httpx.Client(timeout=35.0) as client:
        for c in cases:
            cid = c["id"]
            t0 = time.time()
            try:
                r = client.post(f"{base_url.rstrip('/')}/optimize-energy", json=c["input"])
                elapsed = time.time() - t0
                if r.status_code != 200:
                    print(f"[FAIL] {cid}: HTTP {r.status_code} {str(r.text)[:160]}")
                    failures += 1
                    continue
                response = r.json()
            except Exception as e:  # noqa: BLE001
                print(f"[FAIL] {cid}: request error: {type(e).__name__}")
                failures += 1
                continue

            interp_problems = check_interpretation(
                c["expected_output"]["directive_interpretation"],
                response.get("directive_interpretation", []),
            )
            violations = judge_replay(response, c["input"], response.get("directive_interpretation"))
            cost = response.get("total_cost_bdt", float("nan"))
            ref_cost = c["expected_output"]["total_cost_bdt"]
            ok = not interp_problems and not violations and cost <= ref_cost + TOL
            status = "PASS" if ok else "FAIL"
            print(f"[{status}] {cid:10s} cost={cost:10.2f} ref={ref_cost:10.2f} "
                  f"interp={'OK' if not interp_problems else 'MISMATCH'} "
                  f"valid={'OK' if not violations else 'INVALID'} {elapsed:.2f}s")
            for p in interp_problems[:6]:
                print(f"       interp: {p}")
            for v in violations[:6]:
                print(f"       valid : {v}")
            if not ok:
                failures += 1
    print(f"\nMODE C: {len(cases) - failures}/{len(cases)} passed")
    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=DEFAULT_CASES, help="path to public cases JSON")
    ap.add_argument("--llm", action="store_true", help="also run in-process LLM pipeline")
    ap.add_argument("--base-url", default=None, help="run against a deployed service")
    args = ap.parse_args()

    cases = load_cases(args.cases)
    total_failures = run_local(cases)
    if args.llm:
        total_failures += run_llm(cases)
    if args.base_url:
        total_failures += run_http(cases, args.base_url)

    print("\n" + "=" * 78)
    if total_failures == 0:
        print("ALL CHECKS PASSED — ready to submit")
        sys.exit(0)
    print(f"{total_failures} FAILURE(S) — fix before submitting")
    sys.exit(1)


if __name__ == "__main__":
    main()
