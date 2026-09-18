"""Adversarial API tests: malformed inputs, edge cases, reliability drills.

Run:  python tests/test_api.py        (in-process, uses FastAPI TestClient)
No API key needed — the LLM path degrades to the emergency classifier, which is
exactly what the reliability rubric wants to see (no crash, no 5xx on valid
requests, controlled 400 on invalid ones).
"""
from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "..", "public_cases", "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json")) as f:
    _CASES = json.load(f)["cases"]
VALID = _CASES[0]["input"]

client = TestClient(app, raise_server_exceptions=False)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {extra}")


def post_raw(body):
    return client.post("/optimize-energy", content=body, headers={"Content-Type": "application/json"})


def post(payload):
    return client.post("/optimize-energy", json=payload)


def main():
    # ---- health
    r = client.get("/health")
    check("health 200 + status ok", r.status_code == 200 and r.json() == {"status": "ok"})

    # ---- valid request (emergency interpreter without a key)
    r = post(VALID)
    ok = r.status_code == 200
    body = r.json() if ok else {}
    check("valid request -> 200", ok, f"got {r.status_code}: {str(r.text)[:150]}")
    check("response has all 7 top-level fields", ok and set(body) == {
        "scenario_id", "directive_interpretation", "hourly_plan", "total_grid_kwh",
        "total_cost_bdt", "peak_grid_kwh", "plan_summary"})
    check("scenario_id echoed", ok and body.get("scenario_id") == VALID["scenario_id"])
    check("24-hour plan", ok and len(body.get("hourly_plan", [])) == 24)
    check("one interpretation per note", ok and len(body.get("directive_interpretation", [])) == len(VALID["operator_notes"]))

    # ---- malformed JSON
    r = post_raw("{not valid json")
    check("malformed JSON -> 400", r.status_code == 400, f"got {r.status_code}")
    check("malformed JSON: controlled body, no stack trace", "Traceback" not in r.text and "error" in r.json())

    # ---- structural validation
    cases = [
        ("0 notes -> 400", {**VALID, "operator_notes": []}),
        ("4 notes -> 400", {**VALID, "operator_notes": ["a.", "b.", "c.", "d."]}),
        ("empty note string -> 400", {**VALID, "operator_notes": ["   "]}),
        ("23 hours -> 400", {**VALID, "hours": VALID["hours"][:23]}),
        ("25 hours -> 400", {**VALID, "hours": VALID["hours"] + [dict(VALID["hours"][0])]}),
        ("duplicate hour -> 400", {**VALID, "hours": [dict(h, hour=0 if i == 5 else h["hour"]) for i, h in enumerate(VALID["hours"])]}),
        ("negative demand -> 400", {**VALID, "hours": [dict(h, demand_kwh=-1) if i == 3 else h for i, h in enumerate(VALID["hours"])]}),
        ("missing battery -> 400", {k: v for k, v in VALID.items() if k != "battery"}),
        ("battery initial > capacity -> 400", {**VALID, "battery": {**VALID["battery"], "initial_energy_kwh": 99999}}),
        ("missing scenario_id -> 400", {k: v for k, v in VALID.items() if k != "scenario_id"}),
        ("note not a string -> 400", {**VALID, "operator_notes": [123]}),
    ]
    for name, payload in cases:
        r = post(payload)
        check(name, r.status_code == 400, f"got {r.status_code}")

    # ---- extra fields are tolerated (lenient on unknowns, strict on the contract)
    r = post({**VALID, "extra_metadata": {"foo": 1}})
    check("extra fields tolerated -> 200", r.status_code == 200, f"got {r.status_code}")

    # ---- numbers as strings should NOT pass silently
    r = post({**VALID, "hours": [dict(h, demand_kwh="100") if i == 0 else h for i, h in enumerate(VALID["hours"])]})
    check("string demand rejected -> 400", r.status_code == 400, f"got {r.status_code}")

    # ---- reliability drill: 20 sequential valid requests, all must be 200
    ok_count = 0
    for _ in range(20):
        rr = post(_CASES[_ % 5 if False else 0]["input"] if False else VALID)
        ok_count += rr.status_code == 200
    check("20 sequential valid requests all 200", ok_count == 20, f"{ok_count}/20")

    # ---- all 10 public cases through the API (emergency path) still valid
    from app.validator import judge_replay
    invalid = 0
    for c in _CASES:
        rr = post(c["input"])
        if rr.status_code != 200:
            invalid += 1
            continue
        resp = rr.json()
        v = judge_replay(resp, c["input"], resp.get("directive_interpretation"))
        if v:
            invalid += 1
            print(f"       {c['id']} violations: {v[:3]}")
    check("10 public cases via API: all 200 + GridWise-valid", invalid == 0, f"{invalid} bad")

    # ---- no secrets in any response
    leaked = any("sk-" in json.dumps(r.json()) for r in [post(VALID)] if r.status_code == 200)
    check("no secrets leaked in responses", not leaked)

    print(f"\n{'ALL API TESTS PASSED' if FAIL == 0 else f'{FAIL} FAILURE(S)'}  ({PASS} passed)")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
