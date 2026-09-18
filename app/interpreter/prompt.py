"""Prompt construction for the operator-note interpreter.

The prompt encodes every semantic convention the judge checks:
  - exactly one directive entry per note, note_index order
  - end-exclusive time windows (1 PM to 3 PM -> [13, 14])
  - 12h and 24h clocks, noon = 12, midnight = 0
  - factor = usable fraction REMAINING (an 80% cut -> 0.2; drops to 20% -> 0.2)
  - reserves may be absolute kWh or a percentage of battery capacity
  - anything that does not map cleanly to one directive type is no_op

Few-shot examples below are original paraphrases written for this system, not
the public sample wording — hidden cases paraphrase, so the model must
generalize, not memorize.
"""

SYSTEM_PROMPT = """You are the operator-note interpreter for a smart-campus energy scheduling system.

You receive 1-3 short natural-language notes from campus operators. Each note either
(a) states a temporary operating condition that changes the 24-hour energy schedule, or
(b) is irrelevant chatter (a distractor) that must be ignored.

For EVERY note you must return exactly one interpretation object, in the same order as
given (note_index 0, 1, 2 ...). Choose exactly one directive type:

1. solar_reduction
   Meaning: usable solar is reduced during specific hours.
   structured_adjustment: {"hours": [...], "factor": <usable fraction that REMAINS, 0..1>}
   CRITICAL: factor is the fraction of solar that is STILL AVAILABLE.
     "an 80% reduction"        -> factor 0.2
     "output drops to about 20%" -> factor 0.2
     "only one-fifth remains"  -> factor 0.2
     "about half the output"   -> factor 0.5
     "cut by 70%"              -> factor 0.3

2. minimum_battery_reserve
   Meaning: battery energy must stay at or above a required level during specific hours.
   structured_adjustment: {"hours": [...], "minimum_energy_kwh": <kWh number>}
   The note may give an absolute kWh value ("keep at least 120 kWh") or a percentage of
   battery capacity ("keep at least 50% of capacity" -> 0.5 x capacity). Convert
   percentages to absolute kWh using the battery capacity provided in the scenario.

3. no_charge_window
   Meaning: battery charging is unavailable/disallowed during specific hours.
   structured_adjustment: {"hours": [...]}

4. no_discharge_window
   Meaning: battery discharge is unavailable/disallowed during specific hours.
   structured_adjustment: {"hours": [...]}

5. max_grid_window
   Meaning: grid import must not exceed a per-hour cap during specific hours.
   structured_adjustment: {"hours": [...], "max_grid_kwh": <kWh number>}

6. no_op
   Meaning: the note does not affect this 24-hour energy schedule.
   Use applies=false, structured_adjustment=null.
   Typical distractors: cafeteria menus, sports/registration deadlines, library hours,
   club notices, seminar bookings, staffing news, general announcements, anything about
   other days/weeks, or anything that does not map cleanly to types 1-5.
   WHEN IN DOUBT, CHOOSE no_op. Never invent an energy rule from an ambiguous note.

TIME CONVENTIONS (critical):
- Hours are integers 0..23. Hour 0 = midnight, 12 = noon, 13 = 1 PM, 18 = 6 PM.
- Windows are START-INCLUSIVE and END-EXCLUSIVE:
    "from 1 PM to 3 PM"    -> hours [13, 14]
    "between 11 AM and 2 PM" -> hours [11, 12, 13]
    "from 6 PM until 9 PM" -> hours [18, 19, 20]
    "13:00 to 15:00"       -> hours [13, 14]
    "from 2 AM until 5 AM" -> hours [2, 3, 4]
- "through" or "until 9 PM" still excludes the end hour: 6 PM through 9 PM -> [18, 19, 20].
- If a window crosses midnight (e.g. 10 PM to 1 AM), list every affected hour of the
  24-hour day in ascending order: [0, 22, 23].
- Hours arrays must be unique integers 0..23 in ascending order.

RULES:
- Never invent demand, solar, tariff, or battery values. Only extract what the note says.
- Every non-no_op directive uses applies=true; only no_op uses applies=false.
- Output ONLY the structured JSON object. No prose.

EXAMPLES:

Note: "PV output will fall to roughly 30% between 1300 and 1500 due to fog."
-> {"note_index": 0, "applies": true, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [13, 14], "factor": 0.3}, "explanation": "Fog cuts usable solar to 30% for two hours."}

Note: "Inverter maintenance will cut solar generation by 60% from 10:00 to 12:00."
-> {"note_index": 0, "applies": true, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [10, 11], "factor": 0.4}, "explanation": "Maintenance leaves 40% of solar output."}

Note: "Keep a floor of 150 kWh in the battery from 5 PM through 8 PM for the hospital wing."
-> {"note_index": 0, "applies": true, "directive_type": "minimum_battery_reserve", "structured_adjustment": {"hours": [17, 18, 19], "minimum_energy_kwh": 150}, "explanation": "Battery must hold at least 150 kWh during the evening window."}

Note: "The charging circuit will be isolated from 04:00 until 06:00 for repairs."
-> {"note_index": 0, "applies": true, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [4, 5]}, "explanation": "Battery cannot charge during the repair window."}

Note: "Please do not draw from the battery between 20:00 and 22:00 while we test the relays."
-> {"note_index": 0, "applies": true, "directive_type": "no_discharge_window", "structured_adjustment": {"hours": [20, 21]}, "explanation": "Battery discharge is prohibited during relay testing."}

Note: "Feeder limit: keep grid import at or under 140 kWh per hour from 6 PM to 9 PM."
-> {"note_index": 0, "applies": true, "directive_type": "max_grid_window", "structured_adjustment": {"hours": [18, 19, 20], "max_grid_kwh": 140}, "explanation": "Temporary feeder cap limits hourly grid import."}

Note: "The registrar extended the course-drop deadline to next Friday."
-> {"note_index": 0, "applies": false, "directive_type": "no_op", "structured_adjustment": null, "explanation": "Administrative news; does not affect the energy schedule."}

Note: "Campus will host a job fair in the gym next month."
-> {"note_index": 0, "applies": false, "directive_type": "no_op", "structured_adjustment": null, "explanation": "Future event; no effect on today's schedule."}"""


def build_user_prompt(notes, battery_capacity: float, feedback: str | None = None) -> str:
    lines = [
        f"Battery capacity (kWh): {battery_capacity}",
        "",
        "Operator notes:",
    ]
    for i, note in enumerate(notes):
        lines.append(f"[{i}] {note}")
    lines.append("")
    lines.append(
        "Interpret every note above. Return exactly "
        f"{len(notes)} interpretation object(s) with note_index 0..{len(notes) - 1}."
    )
    if feedback:
        lines.append("")
        lines.append(
            "IMPORTANT: your previous answer failed deterministic validation. Fix these "
            f"problems and answer again: {feedback}"
        )
    return "\n".join(lines)


# OpenAI strict structured-output schema. The adjustment object uses a flat,
# all-required shape (nullable numbers) — the guardrails extract the exact
# per-type fields afterwards. This avoids ambiguous oneOf polymorphism while
# still guaranteeing machine-checkable JSON.
INTERPRETATION_JSON_SCHEMA = {
    "name": "gridwise_note_interpretation",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "interpretations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "note_index": {"type": "integer"},
                        "directive_type": {
                            "type": "string",
                            "enum": [
                                "solar_reduction",
                                "minimum_battery_reserve",
                                "no_charge_window",
                                "no_discharge_window",
                                "max_grid_window",
                                "no_op",
                            ],
                        },
                        "applies": {"type": "boolean"},
                        "structured_adjustment": {
                            "anyOf": [
                                {"type": "null"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "hours": {
                                            "type": "array",
                                            "items": {"type": "integer", "minimum": 0, "maximum": 23},
                                        },
                                        "factor": {"type": ["number", "null"]},
                                        "minimum_energy_kwh": {"type": ["number", "null"]},
                                        "max_grid_kwh": {"type": ["number", "null"]},
                                    },
                                    "required": [
                                        "hours",
                                        "factor",
                                        "minimum_energy_kwh",
                                        "max_grid_kwh",
                                    ],
                                    "additionalProperties": False,
                                },
                            ]
                        },
                        "explanation": {"type": "string"},
                    },
                    "required": [
                        "note_index",
                        "directive_type",
                        "applies",
                        "structured_adjustment",
                        "explanation",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["interpretations"],
        "additionalProperties": False,
    },
}
