"""EMERGENCY-ONLY deterministic note classifier.

This module is the last rung of the safe-failure ladder. The Problem Statement
forbids hard-coded phrase matching as the SOLE interpreter — the LLM is the
required interpretation path (see app/interpreter/llm.py). This classifier runs
only when the LLM and its fallback both fail, so the service still returns a
controlled, valid response instead of crashing (Section 08: SAFE FAILURE).

It is deliberately conservative: any note it cannot confidently parse is
returned as no_op, because an invented directive is worse than a missed one.
"""
from __future__ import annotations

import re
from typing import Optional

_FRACTIONS = {
    "half": 0.5, "quarter": 0.25, "third": 1 / 3, "fifth": 0.2,
    "one-half": 0.5, "one-quarter": 0.25, "one-third": 1 / 3, "one-fifth": 0.2,
    "two-thirds": 2 / 3, "three-quarters": 0.75, "three-fifths": 0.6,
    "two-fifths": 0.4, "one-fourth": 0.25, "three-fourths": 0.75,
}

_REDUCTION_VERBS = (
    r"(?:reduc\w*|cut|cuts|decreas\w*|drop\w*|dip\w*|shrink\w*|lower\w*|"
    r"loss|lose[sr]?|losing|lost|fall[s]?|falling)"
)

_TIME_SEP = r"(?:\s+(?:to|until|till|through|thru|and)\s+|\s*[-\u2013]\s*)"

# time tokens: 1300 / 13:00 / 1:30pm / 2 pm / noon / midnight
_TIME_TOKEN = r"(?:noon|midnight|\d{4}|\d{1,2}(?::\d{2})?\s*(?:a\.m\.|p\.m\.|am|pm)?)"


def _parse_clock(token: str) -> Optional[int]:
    """Parse a time token to hour 0..23. Returns None if not a valid time."""
    t = token.strip().lower().replace(".", "")
    if t == "noon":
        return 12
    if t == "midnight":
        return 0
    # military 4-digit: 1300 -> 13h, 0030 -> 0h (minute folded: 0030 -> hour 0)
    m = re.fullmatch(r"(\d{2})(\d{2})", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh <= 23 and mm <= 59:
            return hh
        return None
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None
    h = int(m.group(1))
    minute = int(m.group(2) or 0)
    mer = m.group(3)
    if h > 23 or minute > 59:
        return None
    if mer == "am":
        return 0 if h == 12 else h
    if mer == "pm":
        return 12 if h == 12 else h + 12
    # bare number (24h style like "15" in "hours 15 through 18")
    return h


def parse_window(note: str) -> Optional[list]:
    """Extract an hour list [start, end) from the note. None if not found."""
    m = re.search(
        rf"(?:from\s+|between\s+|across\s+(?:hours?\s+)?)?({_TIME_TOKEN}){_TIME_SEP}({_TIME_TOKEN})",
        note,
        flags=re.IGNORECASE,
    )
    if m:
        a, b = _parse_clock(m.group(1)), _parse_clock(m.group(2))
        if a is not None and b is not None:
            if b > a:
                return list(range(a, b))
            if b == a:
                return None
            # crosses midnight: 10 PM -> 1 AM == [22, 23, 0]
            return list(range(a, 24)) + list(range(0, b))
    m = re.search(rf"({_TIME_TOKEN})\s+for\s+(\d{1,2})\s+hours?", note, flags=re.IGNORECASE)
    if m:
        a = _parse_clock(m.group(1))
        dur = int(m.group(2))
        if a is not None and 1 <= dur <= 24:
            return [(a + k) % 24 for k in range(dur)]
    return None


# ------------------------------------------------------------ factor extraction

_PCT = r"(\d+(?:\.\d+)?)\s*%"


def _factor_from(note: str) -> Optional[float]:
    """Usable-remaining solar fraction. Order matters: loss phrasings first."""
    low = note.lower()

    def _c(v: float) -> float:
        return max(0.0, min(1.0, v))

    # "reduced/cut/... [filler] by X%"  /  "lose X% of output"
    if re.search(_REDUCTION_VERBS, low) and re.search(
            rf"{_REDUCTION_VERBS}\b[^.;%]{{0,40}}?\bby\s+{_PCT}", low):
        m = re.search(rf"by\s+{_PCT}", low)
        return _c(1.0 - float(m.group(1)) / 100.0)
    if re.search(rf"(?:lose[sr]?|losing|lost|loss of)\s+(?:about\s+|roughly\s+)?{_PCT}", low):
        m = re.search(rf"(?:lose[sr]?|losing|lost|loss of)\s+(?:about\s+|roughly\s+)?{_PCT}", low)
        return _c(1.0 - float(m.group(1)) / 100.0)
    # "X% dip/reduction/decrease/less/lower" (percent BEFORE the noun)
    m = re.search(rf"{_PCT}\s+(?:dip|reduction|decrease|cut|loss|less|lower|shortfall)", low)
    if m:
        return _c(1.0 - float(m.group(1)) / 100.0)
    # availability phrasings: "to 30%", "at 30%", "as 65% of the forecast",
    # "30% of the/normal/forecast output", "30% remains"
    m = re.search(rf"(?:to|at|as|treated as|roughly|about|approximately|around)\s+{_PCT}", low)
    if m:
        return _c(float(m.group(1)) / 100.0)
    m = re.search(rf"{_PCT}\s+of\s+(?:the\s+|normal\s+|expected\s+|typical\s+|nominal\s+|forecast\s+)+(?:forecast|output|production|solar|pv|generation|capacity)?", low)
    if m and re.search(r"(?:forecast|output|production|solar|pv|generation|normal|expected|typical|nominal)", low):
        return _c(float(m.group(1)) / 100.0)
    m = re.search(rf"{_PCT}\s+(?:remains?|remaining|available|usable|is usable)", low)
    if m:
        return _c(float(m.group(1)) / 100.0)
    # fraction words: "halve(s)", "half the output", "one-fifth remains"
    if re.search(r"\bhalves?\b|\bhalved\b", low):
        return 0.5
    for word, frac in sorted(_FRACTIONS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(word)}\b", low):
            return _c(frac)
    return None


def _kwh_value(note: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", note, flags=re.IGNORECASE)
    return float(m.group(1)) if m else None


def _pct_or_fraction_capacity(note: str, capacity: float) -> Optional[float]:
    """'50% of capacity' or 'half the battery capacity' -> absolute kWh."""
    low = note.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?(?:the\s+)?(?:battery\s+)?capacity", low)
    if m:
        return float(m.group(1)) / 100.0 * capacity
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?battery", low)
    if m:
        return float(m.group(1)) / 100.0 * capacity
    if re.search(r"\bcapacity\b", low):
        for word, frac in sorted(_FRACTIONS.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{re.escape(word)}\b", low):
                return round(frac * capacity, 6)
    return None


def _norm(note: str) -> str:
    return " ".join(note.lower().split())


# ------------------------------------------------------------------ classifier

_RE_SOLAR_KW = re.compile(r"\b(solar|pv|photovoltaic|panel|panels|inverter|inverters|array|arrays)\b")
_RE_RESERVE_KW = re.compile(
    r"\b(reserve|reserves|floor|minimum|keep at least|maintain at least|retain at least|"
    r"retain|stays? in|stay in|stored in|remain in|remains in|no lower than|"
    r"not (?:let|allow|go|dip|drop|fall) (?:stored )?(?:energy|level|charge)? ?"
    r"(?:below|under)|dip below)\b"
)
_RE_BATTERY_KW = re.compile(r"\b(battery|batteries|storage|stored)\b")
_RE_NO_KW = re.compile(
    r"\b(no|not|never|off|disabl\w*|unavail\w*|isolat\w*|prohibit\w*|"
    r"prevent\w*|suspend\w*|stop\w*|keep[s]?[^.;]{0,30}?from)\b"
)
_RE_CHARGE_KW = re.compile(r"\bcharg\w*\b")
_RE_DISCHARGE_KW = re.compile(
    r"\b(discharg\w*|draws? from|drawing from|drawn from|draw from the batter|"
    r"suppl(?:y|ying|ies) (?:any|the) (?:load|campus)|battery (?:output|supply))\b"
)
_RE_GRID_KW = re.compile(r"\b(grid|feeder|transformer|substation)\b")
_RE_GRID_LIMIT_KW = re.compile(
    r"\b(import|imports|intake|draw|draws|purchase|purchases|usage|load|limit|"
    r"limits|cap|caps|exceed|exceeds|under|at or below|at or under|at most|"
    r"no more than|up to|within|maximum)\b"
)


def classify(note: str, battery_capacity: float, note_index: int) -> dict:
    """Conservative emergency classification of a single note."""
    low = _norm(note)
    hours = parse_window(note)

    def result(dtype, adj, explanation):
        return {
            "note_index": note_index,
            "applies": dtype != "no_op",
            "directive_type": dtype,
            "structured_adjustment": adj,
            "explanation": explanation,
        }

    # 1) solar reduction (checked first: "grid inspection" must not steal it)
    if _RE_SOLAR_KW.search(low) and re.search(r"\b(solar|pv|photovoltaic|generation|output|production|supply|yield)\b", low):
        factor = _factor_from(note)
        if hours and factor is not None:
            return result(
                "solar_reduction",
                {"hours": hours, "factor": factor},
                "Emergency fallback: solar availability reduced in the stated window.",
            )
        return result("no_op", None, "Solar-related note could not be parsed confidently.")

    # 2) battery reserve floor (battery + at-least language)
    if _RE_BATTERY_KW.search(low) and _RE_RESERVE_KW.search(low):
        if hours is None:
            return result("no_op", None, "Reserve note without a usable time window.")
        kwh = _kwh_value(note)
        if kwh is None:
            kwh = _pct_or_fraction_capacity(note, battery_capacity)
        if kwh is not None:
            return result(
                "minimum_battery_reserve",
                {"hours": hours, "minimum_energy_kwh": kwh},
                "Emergency fallback: battery reserve floor for the stated window.",
            )
        return result("no_op", None, "Reserve note without a usable kWh value.")

    # 3) discharge prohibition (before charge: "discharge" contains "charg")
    if _RE_DISCHARGE_KW.search(low) and _RE_NO_KW.search(low):
        if hours:
            return result(
                "no_discharge_window",
                {"hours": hours},
                "Emergency fallback: discharge prohibited in the stated window.",
            )

    # 4) charge prohibition
    if _RE_CHARGE_KW.search(low) and _RE_NO_KW.search(low):
        if hours:
            return result(
                "no_charge_window",
                {"hours": hours},
                "Emergency fallback: charging unavailable in the stated window.",
            )

    # 5) grid import cap
    if _RE_GRID_KW.search(low) and _RE_GRID_LIMIT_KW.search(low):
        kwh = _kwh_value(note)
        if hours and kwh is not None:
            return result(
                "max_grid_window",
                {"hours": hours, "max_grid_kwh": kwh},
                "Emergency fallback: grid import capped in the stated window.",
            )
        return result("no_op", None, "Grid note could not be parsed confidently.")

    # 6) default: distractor
    return result("no_op", None, "Emergency fallback: note treated as not affecting the schedule.")
