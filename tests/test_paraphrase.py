"""Paraphrase-pack runner: measures interpretation accuracy across wording variants.

Usage:
    OPENAI_API_KEY=sk-... python tests/test_paraphrase.py            # LLM path
    python tests/test_paraphrase.py --emergency                      # fallback classifier only
    python tests/test_paraphrase.py --emergency --verbose

Target before the round: >= 95% on the LLM path, >= 70% on the emergency
classifier (it is only a last-resort safety net, not the primary interpreter).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HERE = os.path.dirname(os.path.abspath(__file__))
TOL = 0.01


def load_pack():
    with open(os.path.join(HERE, "paraphrase_pack.json")) as f:
        return json.load(f)


def parse_multi(spec: str):
    """Parse 'MULTI:0=no_charge_window[2,3,4];1=no_op' into full expected dicts."""
    import re
    out = {}
    for part in spec.replace("MULTI:", "").split(";"):
        idx, desc = part.split("=", 1)
        exp = {"directive_type": desc.split("[", 1)[0]}
        m = re.search(r"\[([\d,\s]+)\]", desc)
        if m:
            exp["hours"] = [int(x) for x in m.group(1).split(",")]
        m = re.search(r"f([\d.]+)", desc)
        if m:
            exp["factor"] = float(m.group(1))
        m = re.search(r"cap([\d.]+)", desc)
        if m:
            exp["max_grid_kwh"] = float(m.group(1))
        m = re.search(r"kwh([\d.]+)", desc)
        if m:
            exp["minimum_energy_kwh"] = float(m.group(1))
        out[int(idx)] = exp
    return out


def entry_matches(expected, entry: dict, capacity: float) -> bool:
    """expected is either a dict (single-note case) or 'MULTI:...' (handled by caller)."""
    if isinstance(expected, str) and expected.startswith("MULTI:"):
        return False  # handled separately
    if expected.get("directive_type") == "no_op":
        return entry["directive_type"] == "no_op" and entry["applies"] is False \
            and entry["structured_adjustment"] is None
    dt = expected["directive_type"]
    if entry["directive_type"] != dt:
        return False
    adj = entry.get("structured_adjustment") or {}
    if adj.get("hours") != sorted(set(expected.get("hours", []))):
        return False
    if dt == "solar_reduction":
        return abs(adj.get("factor", -1) - expected["factor"]) <= TOL
    if dt == "max_grid_window":
        return abs(adj.get("max_grid_kwh", -1) - expected["max_grid_kwh"]) <= TOL
    if dt == "minimum_battery_reserve":
        kwh = expected.get("minimum_energy_kwh")
        if kwh is None and "minimum_energy_pct" in expected:
            kwh = expected["minimum_energy_pct"] / 100.0 * capacity
        return abs(adj.get("minimum_energy_kwh", -1) - kwh) <= max(TOL, TOL * kwh)
    return True  # hours-only types


def run(emergency_only: bool, verbose: bool):
    pack = load_pack()
    capacity = pack["_meta"]["battery_capacity_for_pct"]
    cases = pack["cases"]

    if emergency_only:
        from app.interpreter import emergency
        interpret = lambda notes: ([emergency.classify(n, capacity, i) for i, n in enumerate(notes)], "emergency")
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY not set — falling back to --emergency mode")
            from app.interpreter import emergency
            interpret = lambda notes: ([emergency.classify(n, capacity, i) for i, n in enumerate(notes)], "emergency")
        else:
            from app.interpreter import interpret_notes
            interpret = lambda notes: interpret_notes(notes, capacity)

    stats = defaultdict(lambda: [0, 0])  # type -> [correct, total]
    fails = []
    for case in cases:
        expected = case["expected"]
        is_multi = isinstance(expected, str) and expected.startswith("MULTI:")
        notes = case["notes"] if "notes" in case else [case["note"]]
        note = " ".join(notes)
        entries, _src = interpret(notes)

        if is_multi:
            mapping = parse_multi(expected)
            ok = True
            for idx, exp in mapping.items():
                e = entries[idx]
                if not entry_matches(exp, e, capacity):
                    ok = False
                    fails.append((note, f"note[{idx}] expected {exp}, got {e['directive_type']} {e['structured_adjustment']}"))
            kind = "multi"
            stats[kind][1] += 1
            if ok:
                stats[kind][0] += 1
        else:
            e = entries[0]
            ok = entry_matches(expected, e, capacity)
            kind = expected.get("directive_type") if isinstance(expected, dict) else str(expected)
            stats[kind][1] += 1
            if ok:
                stats[kind][0] += 1
            else:
                fails.append((note, f"expected {expected.get('directive_type')} {expected}, got {e['directive_type']} {e['structured_adjustment']}"))

    total_c = sum(v[0] for v in stats.values())
    total_n = sum(v[1] for v in stats.values())
    print("=" * 78)
    print(f"PARAPHRASE PACK — {'EMERGENCY classifier' if emergency_only else 'LLM path'}")
    print("=" * 78)
    for k in sorted(stats):
        c, n = stats[k]
        print(f"  {k:28s} {c:3d}/{n:3d}  ({100.0 * c / n:5.1f}%)")
    print(f"  {'TOTAL':28s} {total_c:3d}/{total_n:3d}  ({100.0 * total_c / total_n:5.1f}%)")
    if verbose:
        print("\nFailures:")
        for note, why in fails:
            print(f"  - {why}\n    note: {note[:110]}")
    return 0 if total_c == total_n else (0 if (100.0 * total_c / total_n) >= (70 if emergency_only else 95) else 1)


def _split_multi(note: str):
    """Split a combined note into numbered sentences (for MULTI expectations)."""
    import re
    parts = re.split(r"(?<=[.!?])\s+", note.strip())
    return [p for p in parts if p]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--emergency", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    sys.exit(run(args.emergency, args.verbose))
