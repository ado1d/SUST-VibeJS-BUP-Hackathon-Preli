"""Edge-case evaluation suite: LLM -> guardrail -> optimizer -> self-validation.

Backends
--------
openai  Production path (structured outputs) using OPENAI_API_KEY. Use this
        from any network where api.openai.com is reachable, e.g.:
            OPENAI_API_KEY=sk-... python tests/edge_cases.py --backend openai

zai     In-house GLM backend (z-ai CLI) for environments where OpenAI is
        geo-blocked. It monkey-patches the model call but keeps EVERYTHING
        ELSE in the production path: same system prompt, same user prompt,
        same guardrails, same corrective-retry ladder, same optimizer and
        judge replay. GLM has no native structured outputs, so a strict
        JSON-format instruction is appended — this makes the guardrails work
        harder than production, which is exactly what an adversarial suite
        should do.

What each case checks
---------------------
Layer A  interpretation match vs gold (type, applies, hours, factor/reserve/cap)
Layer B  full pipeline: response passes judge_replay (energy balance, effective
         solar, battery bounds + reserve, no-charge/no-discharge windows, grid
         caps, end-of-day neutrality, totals) AND cost equals the deterministic
         reference optimum computed from the GOLD interpretation
         (only when the interpretation matched and the case is feasible)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


# -- backend must be known BEFORE app.config is imported (it reads env once) --
def _argv_backend() -> str:
    if "--backend" in sys.argv:
        i = sys.argv.index("--backend")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    for a in sys.argv:
        if a.startswith("--backend="):
            return a.split("=", 1)[1]
    return "openai"


_BACKEND = _argv_backend()
if _BACKEND == "zai" and not os.environ.get("OPENAI_API_KEY"):
    # interpret_notes skips the LLM path entirely when no key is set; with the
    # zai backend the real OpenAI client is never constructed because the
    # model call is monkey-patched below, so a placeholder key is safe.
    os.environ["OPENAI_API_KEY"] = "zai-backend-placeholder"

# -- env must be set BEFORE app.config is imported --------------------------
# cache ON: the Layer B pipeline re-uses Layer A's successful LLM interpretation
# instead of burning a second API call (kinder to rate limits); emergency
# results are never cached, so Layer B still gets a fresh LLM attempt there.
os.environ.setdefault("CACHE_ENABLED", "1")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.interpreter import llm as llm_mod  # noqa: E402
from app.interpreter import emergency  # noqa: E402
from app.interpreter.prompt import SYSTEM_PROMPT, build_user_prompt  # noqa: E402
from app.planner import optimize_scenario  # noqa: E402
from app.schemas import ScenarioRequest  # noqa: E402
from app.guardrails import build_constraints  # noqa: E402
from app.optimizer import solve_schedule  # noqa: E402
from app.postprocess import (  # noqa: E402
    build_hourly_plan,
    compute_totals,
    finalize_energy,
)
from app.validator import judge_replay  # noqa: E402
from edge_case_data import CASES  # noqa: E402

PUBLIC_JSON = os.path.join(HERE, "..", "public_cases", "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json")
TOL = 0.01

# ---------------------------------------------------------------- contexts

_BASES: dict = {}


def load_bases() -> None:
    with open(PUBLIC_JSON) as f:
        sample = json.load(f)["cases"][0]["input"]
    hours = sample["hours"]
    batt220 = sample["battery"]  # 220 kWh, E0 110, min 40, rates 50/50
    batt250 = dict(batt220, capacity_kwh=250, initial_energy_kwh=125)
    battbig = dict(batt220, max_charge_kwh_per_hour=100, max_discharge_kwh_per_hour=100)
    _BASES["BASE220"] = {"hours": hours, "battery": batt220}
    _BASES["BASE250"] = {"hours": hours, "battery": batt250}
    _BASES["BASEBIG"] = {"hours": hours, "battery": battbig}


# ---------------------------------------------------------------- zai backend

FORMAT_ADDON = (
    "\n\nOUTPUT FORMAT (strict): respond with ONLY one JSON object — no markdown "
    "fences, no prose, nothing before or after it. Shape:\n"
    '{"interpretations": [{"note_index": 0, "directive_type": "solar_reduction", '
    '"applies": true, "structured_adjustment": {"hours": [13, 14], "factor": 0.25, '
    '"minimum_energy_kwh": null, "max_grid_kwh": null}, "explanation": "short sentence"}]}\n'
    "directive_type is one of solar_reduction | minimum_battery_reserve | "
    "no_charge_window | no_discharge_window | max_grid_window | no_op. "
    "structured_adjustment is null ONLY for no_op; otherwise include all four keys "
    "inside it, using null for numbers that do not apply to this directive type."
)


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            return None
    return None


def make_zai_call():
    """Build a drop-in replacement for llm._call_model backed by the z-ai CLI.

    Retries with exponential backoff on failure (the gateway returns HTTP 429
    when called in quick succession)."""
    def call(notes, battery_capacity, model, timeout_s, feedback=None):
        system = SYSTEM_PROMPT
        user = build_user_prompt(notes, battery_capacity, feedback) + FORMAT_ADDON
        last_err = "unknown"
        for attempt in range(6):
            if attempt:
                time.sleep(min(2.0 * attempt, 8.0))
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "out.json")
                try:
                    subprocess.run(
                        ["z-ai", "chat", "-p", user, "-s", system, "-o", out],
                        capture_output=True, text=True, timeout=60,
                    )
                except Exception as e:  # noqa: BLE001
                    last_err = f"bridge error: {type(e).__name__}"
                    continue
                if not os.path.exists(out):
                    last_err = "bridge produced no output file (likely 429)"
                    continue
                try:
                    with open(out) as f:
                        data = json.load(f)
                    content = data["choices"][0]["message"]["content"]
                except Exception:  # noqa: BLE001
                    last_err = "bridge output unreadable"
                    continue
                raw = _extract_json(content)
                if not isinstance(raw, dict):
                    last_err = "could not parse JSON from model output"
                    continue
                interp = raw.get("interpretations")
                if not isinstance(interp, list):
                    last_err = "model returned no interpretations array"
                    continue
                return interp, None
        return None, last_err
    return call


# ---------------------------------------------------------------- comparison

def _num_eq(a, b, tol=1e-6) -> bool:
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= tol


def compare_interpretation(got: list, expected: list) -> list:
    diffs = []
    if len(got) != len(expected):
        return [f"entry count {len(got)} != expected {len(expected)}"]
    for g, e in zip(got, expected):
        i = e["note_index"]
        if g.get("note_index") != i:
            diffs.append(f"[{i}] note_index {g.get('note_index')} != {i}")
        if g.get("directive_type") != e["directive_type"]:
            diffs.append(f"[{i}] type {g.get('directive_type')!r} != {e['directive_type']!r}")
        if g.get("applies") != e["applies"]:
            diffs.append(f"[{i}] applies {g.get('applies')} != {e['applies']}")
        ga, ea = g.get("structured_adjustment"), e["structured_adjustment"]
        if (ea is None) != (ga is None):
            diffs.append(f"[{i}] adjustment null-ness mismatch (got {ga!r})")
            continue
        if ea is None:
            continue
        gh, eh = ga.get("hours"), ea.get("hours")
        if list(gh or []) != list(eh):
            diffs.append(f"[{i}] hours {gh} != {eh}")
        for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if key in ea and ea[key] is not None:
                if not _num_eq(ga.get(key), ea[key]):
                    diffs.append(f"[{i}] {key} {ga.get(key)} != {ea[key]}")
        if not str(g.get("explanation") or "").strip():
            diffs.append(f"[{i}] explanation empty")
    return diffs


# ---------------------------------------------------------------- reference

def reference_cost(hours: list, battery: dict, expected_entries: list):
    """Deterministic optimum under the GOLD interpretation (None if infeasible)."""
    ec = build_constraints(expected_entries, hours, battery)
    result = solve_schedule(hours, battery, ec)
    if result.status != "optimal":
        return None
    eff_solar = [hours[h]["solar_kwh"] * ec.solar_factor.get(h, 1.0) for h in range(24)]
    plan, _ = build_hourly_plan(result, hours, eff_solar)
    finalize_energy(plan, battery["initial_energy_kwh"])
    totals = compute_totals(plan, [h["tariff_bdt_per_kwh"] for h in hours])
    return totals["total_cost_bdt"]


# ---------------------------------------------------------------- one case

def run_case(case: dict) -> dict:
    base = _BASES[case["context"]]
    notes = case["notes"]
    expected = case["expected"]
    out = {"id": case["id"], "source": case["source"], "diffs": [], "enforce_violations": []}

    t0 = time.time()
    entries, source = llm_mod.interpret_notes(notes, base["battery"]["capacity_kwh"])
    out["interp_source"] = source
    out["interp_ms"] = int((time.time() - t0) * 1000)
    out["got"] = entries

    # ---- full pipeline (Layer B) — its response interpretation is what the
    # judge actually scores, so compare THAT against gold as well.
    request = {
        "scenario_id": f"EDGE-{case['id']}",
        "operator_notes": notes,
        "hours": base["hours"],
        "battery": base["battery"],
    }
    t1 = time.time()
    resp = optimize_scenario(ScenarioRequest(**request))
    out["pipeline_ms"] = int((time.time() - t1) * 1000)

    resp_entries = resp["directive_interpretation"]
    out["resp_entries"] = resp_entries
    out["resp_diffs"] = compare_interpretation(resp_entries, expected)
    out["diffs"] += out["resp_diffs"]

    violations = judge_replay(resp, request, resp_entries)
    structural = [v for v in violations
                  if "window" not in v and "cap" not in v and "minimum" not in v]
    out["structural_violations"] = structural
    if case["enforce"]:
        out["enforce_violations"] = [v for v in violations if v not in structural]
    else:
        # stress case: ladder is expected; directive violations are the known cause
        out["enforce_violations"] = []

    # ---- cost vs reference optimum
    ref = reference_cost(base["hours"], base["battery"], expected)
    out["reference_cost"] = ref
    out["cost"] = resp["total_cost_bdt"]
    if not out["resp_diffs"] and ref is not None and case["enforce"]:
        if abs(resp["total_cost_bdt"] - ref) > TOL:
            out["diffs"].append(
                f"cost {resp['total_cost_bdt']} != reference optimum {ref}")

    out["passed"] = not out["diffs"] and not out["structural_violations"] and not out["enforce_violations"]
    return out


# ---------------------------------------------------------------- emergency sweep

def emergency_sweep() -> list:
    """Emergency classifier coverage, normalized through guardrails exactly
    like the production fallback path does (hours sorting, applies coercion)."""
    from app.guardrails import normalize_interpretation
    rows = []
    for case in CASES:
        base = _BASES[case["context"]]
        notes = case["notes"]
        raw = [emergency.classify(n, base["battery"]["capacity_kwh"], i)
               for i, n in enumerate(notes)]
        entries, errs = normalize_interpretation(
            raw, len(notes), base["battery"]["capacity_kwh"])
        if errs:
            rows.append({"id": case["id"], "note_index": -1, "ok": False,
                         "diffs": errs})
            continue
        diffs = compare_interpretation(entries, case["expected"])
        rows.append({"id": case["id"], "note_index": -1, "ok": not diffs,
                     "diffs": diffs})
    return rows


# ---------------------------------------------------------------- public LLM regression

def public_llm_regression() -> list:
    with open(PUBLIC_JSON) as f:
        cases = json.load(f)["cases"]
    rows = []
    for c in cases:
        inp = c["input"]
        t0 = time.time()
        entries, source = llm_mod.interpret_notes(
            inp["operator_notes"], inp["battery"]["capacity_kwh"])
        ms = int((time.time() - t0) * 1000)
        gold = c["expected_output"]["directive_interpretation"]
        diffs = compare_interpretation(entries, gold)
        rows.append({"id": c["id"], "source": source, "ms": ms,
                     "ok": not diffs, "diffs": diffs})
    return rows


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["openai", "zai"], default="openai")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--sleep-sec", type=float, default=0.0,
                    help="pause between cases (pacing for rate-limited gateways)")
    ap.add_argument("--public-only", action="store_true",
                    help="run only the 10 public cases through the LLM backend")
    ap.add_argument("--emergency-only", action="store_true")
    ap.add_argument("--include-public", action="store_true",
                    help="also run the 10 public cases through the LLM backend")
    ap.add_argument("--report", help="write JSON report to this path")
    args = ap.parse_args()

    if args.backend == "zai":
        llm_mod._call_model = make_zai_call()
        print("backend: zai (GLM via z-ai CLI, no native structured outputs)")
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY is not set — cannot run the openai backend.")
            return 2
        print(f"backend: openai (model={os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')})")

    load_bases()

    if args.public_only:
        print("public-sample LLM regression:")
        rows = public_llm_regression()
        for row in rows:
            mark = "PASS" if row["ok"] else "FAIL"
            print(f"  {row['id']:<11}{mark}  ({row['source']}, {row['ms']} ms)")
            if not row["ok"]:
                for d in row["diffs"]:
                    print(f"    {d}")
        n_pub = sum(1 for x in rows if x["ok"])
        print(f"public regression: {n_pub}/{len(rows)}")
        if args.report:
            with open(args.report, "w") as f:
                json.dump({"backend": args.backend, "public": rows}, f, indent=2, default=str)
        return 0 if n_pub == len(rows) else 1

    if args.emergency_only:
        rows = emergency_sweep()
        bad = [r for r in rows if not r["ok"]]
        for r in bad:
            print(f"  EMERGENCY MISS {r['id']}: {r['diffs']}")
        print(f"emergency classifier: {len(rows) - len(bad)}/{len(rows)} cases match gold")
        return 0 if not bad else 1

    selected = CASES
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        selected = [c for c in CASES if c["id"] in wanted]

    print(f"running {len(selected)} edge cases...\n")
    results = []
    header = f"{'CASE':<11}{'INTERP':<8}{'ENFORCE':<9}{'COST vs REF':<22}{'LLM ms':<8}{'SRC':<10}VERDICT"
    print(header)
    print("-" * len(header))
    for case in selected:
        if results and args.sleep_sec:
            time.sleep(args.sleep_sec)
        r = run_case(case)
        results.append(r)
        interp_ok = "OK" if not r["diffs"] else "FAIL"
        enforce_ok = ("OK" if not r["enforce_violations"]
                      else "FAIL" if case["enforce"] else "n/a")
        if case["enforce"]:
            cost = (f"{r['cost']:.1f} vs {r['reference_cost']:.1f}"
                    if r["reference_cost"] is not None else "ref infeasible")
        else:
            cost = f"{r['cost']:.1f} (ladder)"
        verdict = "PASS" if r["passed"] else "FAIL"
        print(f"{r['id']:<11}{interp_ok:<8}{enforce_ok:<9}{cost:<22}"
              f"{r['interp_ms']:<8}{r['interp_source']:<10}{verdict}")

    n_pass = sum(1 for r in results if r["passed"])
    print("-" * len(header))
    print(f"RESULT: {n_pass}/{len(results)} passed")

    for r in results:
        if not r["passed"]:
            print(f"\n--- {r['id']} ---")
            for d in r["diffs"]:
                print(f"  interp: {d}")
            for v in r["structural_violations"]:
                print(f"  structural: {v}")
            for v in r["enforce_violations"]:
                print(f"  enforce: {v}")
            if not r["diffs"] and not r["structural_violations"] and not r["enforce_violations"]:
                print("  (cost mismatch)")
            print(f"  got: {json.dumps([{k: e[k] for k in ('note_index','directive_type','structured_adjustment')} for e in r['got']])}")

    public_rows = None
    if args.include_public:
        print("\npublic-sample LLM regression:")
        public_rows = public_llm_regression()
        for row in public_rows:
            mark = "PASS" if row["ok"] else "FAIL"
            print(f"  {row['id']:<11}{mark}  ({row['source']}, {row['ms']} ms)")
            if not row["ok"]:
                for d in row["diffs"]:
                    print(f"    {d}")
        n_pub = sum(1 for x in public_rows if x["ok"])
        print(f"public regression: {n_pub}/{len(public_rows)}")

    if args.report:
        with open(args.report, "w") as f:
            json.dump({"backend": args.backend, "results": results,
                       "public": public_rows}, f, indent=2, default=str)
        print(f"\nreport written to {args.report}")

    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
