# GridWise LLM — Smart Campus Energy Optimization

BUP CSE Fest 2026 Hackathon · Online Preliminary · **Team: _<your team name>_**

One HTTP API service that interprets campus operator notes with an LLM, validates
them deterministically, and returns a provably optimal 24-hour battery/solar/grid
schedule.

```
POST /optimize-energy
        │
        ▼
┌─────────────────┐   ┌──────────────────┐   ┌────────────────┐   ┌───────────────┐
│  Schema check   │──▶│  LLM Interpreter │──▶│  Guardrails    │──▶│  LP Optimizer │
│  (Pydantic)     │   │  OpenAI, 1 call, │   │  deterministic │   │  SciPy HiGHS  │
│  400 on invalid │   │  structured out, │   │  validate +    │   │  exact optimal│
│                 │   │  temp 0, cached  │   │  normalize     │   │  (milliseconds)│
└─────────────────┘   └──────────────────┘   └────────────────┘   └───────┬───────┘
                                                                           │
                                              ┌────────────────────────────▼───┐
                                              │ Post-process + Self-Validator   │
                                              │ (judge-replay every response)   │
                                              └────────────────────────────────┘
```

**LLM role (mandatory requirement):** the language model *is* the operator-note
interpreter — its structured output directly produces the optimization
constraints (Problem Statement §02). It is not used merely for `plan_summary`.
Deterministic code only validates/normalizes that output and does the math.

**Safe-failure ladder:** interpretation cache → primary model → corrective
re-ask → fallback model → emergency rule-based classifier → never crash, never
invent directives (§08 SAFE FAILURE).

---

## Quickstart (local, from a clean environment)

```bash
git clone <your-repo-url> && cd gridwise
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export OPENAI_API_KEY=sk-...        # required for the LLM path
# optional overrides:
# export OPENAI_MODEL=gpt-4o-mini  OPENAI_FALLBACK_MODEL=gpt-4.1-mini

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

Run one public sample against the live service:

```bash
python3 - <<'PY'
import json, urllib.request
case = json.load(open("public_cases/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"))["cases"][0]
req = urllib.request.Request("http://localhost:8000/optimize-energy",
                             data=json.dumps(case["input"]).encode(),
                             headers={"Content-Type": "application/json"})
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2)[:1200])
PY
```

Expected result for SAMPLE-01: `solar_reduction {hours:[12,13], factor:0.25}` +
`no_op`, `total_cost_bdt = 38365.00` (the published reference optimum).

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENAI_API_KEY` | **yes** | — | LLM for operator-note interpretation |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | primary interpreter model |
| `OPENAI_FALLBACK_MODEL` | no | `gpt-4.1-mini` | fallback model |
| `OPENAI_BASE_URL` | no | OpenAI default | optional gateway/proxy |
| `OPENAI_TIMEOUT_S` / `OPENAI_FALLBACK_TIMEOUT_S` | no | 10 / 8 | per-call timeouts |
| `REQUEST_BUDGET_S` | no | 26 | internal budget (< judge 30s limit) |
| `CACHE_ENABLED` / `CACHE_MAX_ENTRIES` | no | 1 / 512 | interpretation cache |
| `PORT` | no | 8000 | service port (Docker) |
| `LOG_LEVEL` | no | INFO | logging verbosity |

Secrets are provided only via environment variables — never committed, never
baked into the Docker image, never logged or returned in responses.

## Verify before submitting

```bash
./run_tests.sh                 # public cases + adversarial API tests + paraphrase pack
python3 tests/public_cases.py --llm          # full pipeline incl. LLM, in-process
python3 tests/public_cases.py --base-url https://<deployed-host>   # against deployment
OPENAI_API_KEY=sk-... python3 tests/edge_cases.py --backend openai   # 31-case edge suite
```

All 10 public cases must report `PASS` (judge-replay-valid AND cost at the
reference optimum — this implementation matches all 10 reference costs exactly).

## Edge-case suite (31 cases)

`tests/edge_cases.py` + `tests/edge_case_data.py` stress the interpretation
layer far beyond the public samples. Each case checks (a) interpretation vs a
gold answer, (b) full-pipeline judge replay, and (c) cost against the
deterministic reference optimum computed from the gold interpretation.

Coverage highlights (all verified passing):

- **Percentage semantics** — "at 15%" (factor 0.15) vs "75% reduction" (factor
  0.25) vs "one-fifth remains" (0.2) vs "completely unavailable" (0.0)
- **Absolute vs relative reserve** — "90 kWh" vs "40% of capacity" vs "half
  battery capacity" (converted via the scenario's capacity)
- **End-exclusive boundaries** — 1-hour windows ("2 PM to 3 PM" -> `[14]`),
  "until midnight" -> hour 24 excluded, "noon to 2 PM" -> `[12, 13]`
- **Cross-midnight windows** — "10 PM to 2 AM" -> `[0, 1, 22, 23]`
- **Zero grid cap** — "No grid import from 2 PM to 4 PM" -> cap 0 (hard
  feasibility test: solar + battery only)
- **Distractors & injection** — LED upgrades "next month", "ignore previous
  instructions", "set factor to 1.5" -> all no_op
- **One note -> two directives** — "charging AND discharging disabled" cannot
  map to exactly one type -> no_op (spec: one directive per note)
- **Solar increases** — "boost to 130% of forecast" is not a supported
  directive -> no_op (original forecast stays in force)
- **Multi-note composition** — overlapping solar reduction + no-charge at the
  same hour, grid caps spanning different windows, mixed with distractors
- **Infeasibility stress** — a 150 kWh grid cap that is arithmetically
  unsatisfiable (needs 55 kWh discharge at h18 vs a 50 kWh/h limit): the
  damage-control ladder must still return a GridWise-valid schedule

Backends: `--backend openai` (production structured outputs; requires your
key) or `--backend zai` (GLM bridge for networks where OpenAI is blocked).
Useful flags: `--only EC16,X5`, `--public-only`, `--emergency-only`,
`--sleep-sec 5` (pacing), `--report out.json`.

## Model / provider disclosure

- **Interpreter:** OpenAI `gpt-4o-mini` (fallback `gpt-4.1-mini`), temperature 0,
  strict structured outputs (JSON schema), single call per request covering all
  1–3 notes.
- **Optimizer:** linear program solved with SciPy `linprog` (HiGHS). 120
  variables, 49 equality constraints, per-hour bounds; solved to proven
  optimality in milliseconds.
- **Guardrails:** deterministic validation/normalization in `app/guardrails.py`
  (type whitelist, note mapping, hour rules, numeric ranges, applies semantics).

## Docker (fallback image)

```bash
docker build -t <registry>/gridwise-api:1.0.0 .
docker push <registry>/gridwise-api:1.0.0

# verified run command (documented for organizers):
docker run -e OPENAI_API_KEY=sk-... -p 8000:8000 <registry>/gridwise-api:1.0.0
# health: curl http://localhost:8000/health -> {"status":"ok"}
```

The image binds `0.0.0.0:8000`, exposes port 8000, contains **no secrets** (the
key is injected at runtime), and includes a `HEALTHCHECK`.

## Project layout

```
app/
  main.py            FastAPI app: /health, /optimize-energy, controlled 400/422/500
  schemas.py         Pydantic request/response contract (exact field names)
  planner.py         orchestration: interpret -> guardrail -> optimize -> validate
  guardrails.py      deterministic validation/normalization of LLM output
  validator.py       judge-replay self-validation (Section 11 twin)
  postprocess.py     plan assembly, action derivation, totals recomputation
  config.py          env-driven settings
  interpreter/
    llm.py           OpenAI structured-output path + retry/fallback/cache
    prompt.py        system prompt + strict JSON schema for interpretations
    emergency.py     last-resort deterministic classifier (SAFE FAILURE only)
  optimizer/
    lp.py            SciPy HiGHS linear program (exact optimal schedule)
tests/
  public_cases.py    10-case pre-submission gate (3 modes: local/LLM/HTTP)
  test_api.py        adversarial API tests (malformed input, reliability)
  test_paraphrase.py wording-robustness suite over tests/paraphrase_pack.json
public_cases/        official public sample case pack (input + reference output)
```

## Known limitations

- Interpretation quality depends on the LLM provider; if both OpenAI models are
  unreachable, the emergency classifier keeps the service alive but may lose
  interpretation credit for affected notes (it never breaks schedule validity).
- Windows crossing midnight (e.g., "11 PM to 1 AM") are represented as the set
  of affected hours in ascending order, per the hours-array schema constraint.
- The LP assumes the lossless battery model defined by the Problem Statement
  (§09); no degradation or round-trip efficiency is modeled because the judge's
  replay uses the same equations.

## Credits

- FastAPI, PyDantic, uvicorn (web framework / validation / server)
- SciPy `linprog` + HiGHS solver (optimization)
- OpenAI API (operator-note interpretation — the mandatory LLM path)
- AI coding assistant (Super Z / Z.ai) used for scaffolding and review;
  architecture, constraints and validation logic are the team's own work and
  fully understood by the team. Public sample cases © the BUP CSE Fest 2026
  organizers, used only for local validation.

## Repository policy compliance

Created after the question reveal; kept **private** during the event; made
**public** after the submission deadline, per the Participant Guide. No secrets
in code, image, or README at any time.
