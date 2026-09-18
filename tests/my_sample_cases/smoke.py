"""Smoke-test freshly generated cases against the live API."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/optimize-energy"
HERE = os.path.dirname(os.path.abspath(__file__))
CASES_PATH = os.path.join(HERE, "gridwise_my_cases.json")


def post(payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read()), ""
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, None, repr(e)


def main():
    with open(CASES_PATH) as f:
        cases = json.load(f)["cases"]
    fail = 0
    for c in cases:
        payload = c["input"]
        status, body, err = post(payload)
        print(f"\n=== {c['id']} - {c['label']} ===")
        print(f"  status: {status}")
        if status != 200 or body is None:
            print(f"  FAIL: {err[:300]}")
            fail += 1
            continue
        hours = body["hourly_plan"]
        di = body["directive_interpretation"]
        notes = payload["operator_notes"]
        init = payload["battery"]["initial_energy_kwh"]
        end = hours[-1]["battery_energy_after_kwh"]
        print(f"  notes: {len(notes)} | directives: {len(di)} | plan hours: {len(hours)}")
        print(f"  battery start->end: {init} -> {end}  (delta={end - init:+.2f})")
        print(f"  total_cost_bdt: {body['total_cost_bdt']}  peak: {body['peak_grid_kwh']}")
        print(f"  directive types: {[d['directive_type'] for d in di]}")
        if len(hours) != 24:
            print("  FAIL: plan not 24 hours"); fail += 1
        if len(di) != len(notes):
            print("  FAIL: directive count != note count"); fail += 1
        if abs(end - init) > 0.01:
            print("  FAIL: end-of-day battery not neutral"); fail += 1
        for d in di:
            if d["directive_type"] == "no_op" and (d["applies"] is not False or d["structured_adjustment"] is not None):
                print(f"  FAIL: no_op must have applies=false and null adjustment: {d}"); fail += 1
            if d["directive_type"] != "no_op" and d["applies"] is not True:
                print(f"  FAIL: non-no_op must have applies=true: {d}"); fail += 1
    print(f"\n{'ALL OK' if fail == 0 else f'{fail} FAILURE(S)'}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
