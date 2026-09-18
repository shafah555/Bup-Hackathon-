# GridWise — Smart Campus Energy Optimization

> LLM-Assisted Operator Directive Interpretation for the **BUP CSE Fest 2026 Online Preliminary Round**.

GridWise is a single HTTP service that turns free-form operator notes into a
**validated 24-hour microgrid schedule**.  Operator notes are interpreted by an
LLM, the LLM's output passes through deterministic guardrails, and a linear
program produces the optimal cost-minimizing schedule.  The schedule is then
independently validated before any HTTP response is sent.

---

## 1. Project Overview

The API accepts:

* a `scenario_id`
* 1–3 free-form `operator_notes`
* exactly 24 hourly energy records
* a battery configuration

It returns:

1. machine-checkable interpretation for **every** operator note
2. a valid 24-hour energy schedule (`hourly_plan`)
3. `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`
4. a short `plan_summary`

The LLM **must** be part of the interpretation path.  Its output is never
trusted: every directive is validated against a strict schema, the merged
constraint set is solved with a deterministic LP, and the resulting schedule
is replayed by an independent validator before being returned to the client.

---

## 2. Architecture

```
Operator Notes (free-form)
        │
        ▼
 ┌────────────────────────┐
 │  LLM Interpreter        │  system prompt + JSON-mode
 │  (llm_interpreter.py)   │  returns raw JSON
 └────────────┬───────────┘
              ▼
 ┌────────────────────────┐
 │  Guardrails             │  schema, types, ranges, hours
 │  (guardrails.py)        │  raises on violation
 └────────────┬───────────┘
              ▼
 ┌────────────────────────┐
 │  Directives Merge       │  per-hour unified constraints
 │  (directives.py)        │
 └────────────┬───────────┘
              ▼
 ┌────────────────────────┐
 │  Optimizer (PuLP LP)    │  minimize sum(grid*tariff)
 │  (optimizer.py)         │  energy balance + directives + neutrality
 └────────────┬───────────┘
              ▼
 ┌────────────────────────┐
 │  Validator              │  replays the plan independently
 │  (validator.py)         │  raises on violation
 └────────────┬───────────┘
              ▼
        JSON Response
```

---

## 3. Data Flow

1. **Pydantic validation** of the request payload (`schemas.py`).
2. **LLM interpretation** (`llm_interpreter.py`):
   * `SYSTEM_PROMPT` enumerates the six canonical directive types, the time
     rules, the solar reduction semantics, and the output schema.
   * `interpret(notes)` calls the model in JSON mode and parses the output.
   * On any failure: an `InterpreterError` is raised.  When
     `OFFLINE_FALLBACK=true` (default), a deterministic `no_op` per note is
     produced so the service stays responsive during local development.
3. **Guardrails** (`guardrails.py`): every JSON item is checked against the
   canonical schema.  Invalid LLM output raises `GuardrailError`.
4. **Directive merge** (`directives.py`): builds `MergedDirectives`, applying
   the *most restrictive* constraint per hour.
5. **Optimization** (`optimizer.py`): a 24-hour LP that minimizes total cost
   subject to:
   * `grid + solar_used + discharge == demand + charge` per hour
   * `solar_used <= effective_solar`
   * battery transitions, capacity, base reserve, charge/discharge rate limits
   * end-of-day neutrality (`E_after[23] == initial`)
   * directive constraints (`no_charge`, `no_discharge`, `reserve`, `max_grid`)
   * mutual exclusivity of charge and discharge
6. **Validation** (`validator.py`): the plan is replayed independently:
   * energy balance
   * battery transitions and capacity
   * directive constraints
   * end-of-day neutrality
   * reported totals (`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`)
     are recomputed from the plan and compared against the API response.
7. **Response** is built from the validated plan; totals are recomputed
   independently by `calculations.plan_totals`.

---

## 4. Canonical Directive Types

| `directive_type`          | `structured_adjustment`                                     | Effect                                                       |
| ------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `solar_reduction`         | `{ "hours": [..], "factor": 0..1 }`                          | `effective_solar[h] = solar[h] * factor` for those hours     |
| `minimum_battery_reserve` | `{ "hours": [..], "minimum_energy_kwh": >=0 }`               | `E_after[h] >= max(base_min, directive_min)` for those hours |
| `no_charge_window`        | `{ "hours": [..] }`                                          | `charge[h] = 0` for those hours                              |
| `no_discharge_window`     | `{ "hours": [..] }`                                          | `discharge[h] = 0` for those hours                           |
| `max_grid_window`         | `{ "hours": [..], "max_grid_kwh": >=0 }`                     | `grid[h] <= max_grid_kwh` for those hours                    |
| `no_op`                   | `null`                                                       | no effect                                                    |

`factor` is the **usable fraction remaining**, not the percentage reduction.
`"solar drops to 20%"` ⇒ `factor = 0.20`.  `"80% reduction"` ⇒
`factor = 0.20`.

For `no_op`:
* `applies` MUST be `false`
* `directive_type` MUST be `"no_op"`
* `structured_adjustment` MUST be `null`

For every other directive type:
* `applies` MUST be `true`

---

## 5. Time Interpretation

* Hours are integers in `[0, 23]`.
* Start hour is **inclusive**, end hour is **exclusive**.
* `"1 PM to 3 PM"` ⇒ `[13, 14]`
* `"2 PM to 5 PM"` ⇒ `[14, 15, 16]`
* `"6 PM to 9 PM"` ⇒ `[18, 19, 20]`
* Normalized to a sorted, deduplicated list.

---

## 6. API Endpoints

| Method | Path                | Body                       | Response               |
| ------ | ------------------- | -------------------------- | ---------------------- |
| GET    | `/health`           | —                          | `{ "status": "ok" }`   |
| POST   | `/optimize-energy`  | `OptimizeRequest` (below)  | `OptimizeResponse`     |

`POST /optimize-energy` request schema:

```json
{
  "scenario_id": "scenario-001",
  "operator_notes": ["...", "..."],
  "hours": [
    { "hour": 0, "demand_kwh": 80, "solar_kwh": 0, "tariff_bdt_per_kwh": 6 },
    "..."
  ],
  "battery": {
    "capacity_kwh": 500,
    "initial_energy_kwh": 200,
    "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100,
    "max_discharge_kwh_per_hour": 100
  }
}
```

`POST /optimize-energy` response schema:

```json
{
  "scenario_id": "scenario-001",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": { "hours": [13, 14], "factor": 0.2 },
      "explanation": "Solar reduced to 20% during the 1-3 PM window."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 80,
      "solar_used_kwh": 0,
      "battery_action": "idle",
      "battery_kwh": 0,
      "battery_energy_after_kwh": 200
    }
  ],
  "total_grid_kwh": 1234.5,
  "total_cost_bdt": 8421.0,
  "peak_grid_kwh": 95.0,
  "plan_summary": "GridWise schedule applies solar reduction in hours [13, 14]; ..."
}
```

`battery_action` is one of `charge`, `discharge`, `idle`.  When `idle`,
`battery_kwh` MUST be `0`.  `grid_kwh`, `solar_used_kwh`, `battery_kwh`,
`battery_energy_after_kwh` are all non-negative.

---

## 7. Environment Variables

Create `.env` from `.env.example`:

```ini
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=
LLM_API_BASE=
LLM_TIMEOUT_SECONDS=20
LLM_TEMPERATURE=0.0

SOLVER_TIME_LIMIT_SECONDS=15
OFFLINE_FALLBACK=true
APP_HOST=0.0.0.0
APP_PORT=8000
VALIDATOR_TOLERANCE=0.01
LOG_LEVEL=INFO
```

> `LLM_API_KEY` MUST never be committed.  The repo ships `.env.example`
> with empty values only.

`OFFLINE_FALLBACK` (default `true`) controls whether the interpreter
falls back to `no_op` for every note when the LLM call fails.  Set it to
`false` in production to surface interpreter failures as HTTP errors.

---

## 8. Local Quickstart

```bash
git clone <your-fork-url>
cd <repo>
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env to fill in LLM_API_KEY etc.

./run.sh                           # Windows: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Verify the service:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Submit a request (uses a public sample case):

```bash
python - <<'PY'
import json, urllib.request

case = json.load(open("sample/requests.json"))["cases"][0]   # baseline case
req = urllib.request.Request(
    "http://localhost:8000/optimize-energy",
    data=json.dumps(case).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
resp = urllib.request.urlopen(req, timeout=30)
print(json.dumps(json.loads(resp.read()), indent=2)[:2000])
PY
```

---

## 9. Run with Docker

```bash
docker build -t gridwise .
docker run --env-file .env -p 8000:8000 gridwise
curl http://localhost:8000/health
```

Or:

```bash
docker compose up --build
```

---

## 10. Testing

The suite is fully LLM-independent — `tests/conftest.py` replaces the LLM
interpreter with a deterministic stub before any test runs.

```bash
pip install -r requirements.txt
python -m pytest -q
```

Coverage includes:

* health endpoint
* schema validation (every input rule)
* every directive type
* time parsing and percentage interpretation
* guardrail failure paths
* optimizer behaviour, end-of-day neutrality, energy balance
* validator replay and total mismatch detection
* all 10 public sample cases from `sample/requests.json`

---

## 11. Optimization Methodology

We use [PuLP](https://pypi.org/project/PuLP/) with the bundled CBC solver.
The problem is a small (5 × 24 = 120 variables), continuous LP that solves
in well under 1 second on commodity hardware.

* Decision variables per hour: `grid`, `solar_used`, `charge`, `discharge`,
  `battery_energy`.
* Objective: minimize `sum(grid[h] * tariff[h])`.
* Constraints encode the energy balance, solar availability, battery
  dynamics, directive constraints, and end-of-day neutrality.
* `charge * discharge == 0` is added as a non-convex constraint to keep
  the API's `battery_action` clean (`idle` / `charge` / `discharge`).

Solar curtailment is allowed; grid export is not.

---

## 12. Battery Model

```
charge    : E_after = E_before + battery_kwh
discharge : E_after = E_before - battery_kwh
idle      : E_after = E_before, battery_kwh = 0
```

Battery constraints:

* `minimum_energy_kwh <= E_after <= capacity_kwh`
* `E_after[23] == initial_energy_kwh` (end-of-day neutrality)
* `battery_kwh <= max_charge_kwh_per_hour` (when charging)
* `battery_kwh <= max_discharge_kwh_per_hour` (when discharging)

Directive reserves raise the lower bound on `E_after` for the affected hours.

---

## 13. Energy Balance

For every hour:

```
grid_kwh + solar_used_kwh + discharge_kwh  ==  demand_kwh + charge_kwh
```

with

```
0 <= solar_used_kwh <= effective_solar_kwh
0 <= grid_kwh
```

---

## 14. Security & Secrets

* No credentials are ever hard-coded.
* `.env` is git-ignored.
* Error responses never include API keys, tokens, or sensitive config.
* `.env.example` ships with empty values.

---

## 15. Failure Handling

| Failure                                  | Behaviour                                                |
| ---------------------------------------- | -------------------------------------------------------- |
| Malformed JSON request                   | HTTP 422 `validation_error`                              |
| Missing or invalid fields                | HTTP 422 `validation_error`                              |
| LLM call times out / fails               | `InterpreterError` (or `no_op` fallback if enabled)      |
| LLM returns invalid JSON / wrong schema  | `InterpreterError` (the API never invents a directive)   |
| Optimizer infeasible                     | HTTP 500 `optimizer_error`                               |
| Validator replays disagree with totals   | HTTP 500 `validator_error`                               |
| Internal exception                       | HTTP 500 `internal_error` (no stack trace leaked)        |

The server never crashes; every error path returns a structured JSON error.

---

## 16. Dependencies

* [FastAPI](https://fastapi.tiangolo.com/) — HTTP framework.
* [Pydantic](https://docs.pydantic.dev/) — request/response validation.
* [PuLP](https://pypi.org/project/PuLP/) — LP modeling (CBC solver bundled).
* [uvicorn](https://www.uvicorn.org/) — ASGI server.
* [pytest](https://docs.pytest.org/) — testing.

---

## 17. Known Limitations

* The `LLMInterpreter` defaults to a conservative `no_op` fallback when the
  LLM is unreachable.  For a serious submission, configure `LLM_API_KEY` and
  set `OFFLINE_FALLBACK=false`.
* PuLP/CBC solves this LP in milliseconds; if you swap in a heavier solver,
  the `SOLVER_TIME_LIMIT_SECONDS` budget protects the 30-second SLA.
* The `charge * discharge == 0` mutual-exclusivity constraint is added for
  cleanliness; it slightly enlarges the LP but keeps the API response
  deterministic in its `battery_action` labelling.

---

## 18. Project Layout

```
gridwise/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── schemas.py
│   ├── llm_interpreter.py
│   ├── guardrails.py
│   ├── directives.py
│   ├── optimizer.py
│   ├── validator.py
│   ├── calculations.py
│   └── errors.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── _helpers.py
│   ├── test_health.py
│   ├── test_schema.py
│   ├── test_interpreter.py
│   ├── test_guardrails.py
│   ├── test_optimizer.py
│   ├── test_validator.py
│   └── test_public_cases.py
├── sample/
│   └── requests.json
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
├── run.sh
└── README.md
```

---

## 19. Robustness Against Hidden Tests

* The interpreter prompt explicitly enumerates allowed directive types and
  forbids unsupported ones.
* Hours are always normalized to a sorted, deduplicated list of integers
  in `[0, 23]` with end-exclusive semantics.
* Solar percentage semantics (usable fraction vs reduction) are spelled out
  in the prompt and re-checked in guardrails.
* Multiple compatible directives are merged to the most restrictive setting
  per hour; nothing is silently discarded.
* The optimizer and validator are deterministic: the same input always
  yields the same schedule (modulo CBC tie-breaking) and the same totals.
* No case-specific values are hard-coded; the test suite is data-driven
  from `sample/requests.json`.

Good luck, and may your grid stay balanced. ⚡
