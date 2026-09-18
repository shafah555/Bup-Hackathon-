"""End-to-end tests driven by the public sample cases JSON.

The tests load ``sample/requests.json`` dynamically, run every case through
``/optimize-energy``, and verify:
    * directive interpretation covers every note in order
    * the schedule is structurally valid
    * battery rules, energy balance and end-of-day neutrality hold
    * directive constraints are respected

We do NOT byte-compare expected schedules: the judge explicitly allows
equivalent optimal schedules.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import DirectiveInterpretation
from tests._helpers import set_stub_interpretations


SAMPLE_FILE = Path(__file__).resolve().parents[1] / "sample" / "requests.json"


client = TestClient(app)


def _load_cases():
    if not SAMPLE_FILE.exists():
        return []
    data = json.loads(SAMPLE_FILE.read_text())
    return data["cases"]


# -- Deterministic interpretations derived from each case --
#
# We map the well-known public cases to deterministic structured directives
# here.  This is intentionally hard-coded per-case-id so the tests don't
# rely on a real LLM call, but the production code path still goes through
# ``LLMInterpreter.interpret`` (stubbed via the conftest fixture).

_INTERPRETATIONS = {
    "case-1-baseline": [
        DirectiveInterpretation(
            note_index=0, applies=False, directive_type="no_op",
            structured_adjustment=None, explanation="baseline no-op"
        )
    ],
    "case-2-solar-drop": [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="solar_reduction",
            structured_adjustment={"hours": [13, 14], "factor": 0.2},
            explanation="PV reduced to 20% in [13, 14]"
        )
    ],
    "case-3-no-discharge-evening": [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="no_discharge_window",
            structured_adjustment={"hours": [18, 19, 20]},
            explanation="no discharge during [18, 20)"
        )
    ],
    "case-4-reserve-evening": [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="minimum_battery_reserve",
            structured_adjustment={"hours": [18, 19, 20, 21], "minimum_energy_kwh": 150.0},
            explanation="reserve at least 150 kWh in [18, 22)"
        )
    ],
    "case-5-grid-cap-peak": [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="max_grid_window",
            structured_adjustment={"hours": [17, 18, 19, 20], "max_grid_kwh": 90.0},
            explanation="grid capped at 90 kWh in [17, 21)"
        )
    ],
    "case-6-no-charge-day": [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="no_charge_window",
            structured_adjustment={"hours": list(range(24))},
            explanation="no charge the entire day"
        )
    ],
    "case-7-combined": [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="solar_reduction",
            structured_adjustment={"hours": [13, 14], "factor": 0.2},
            explanation="80% reduction => 20% remaining in [13, 15)"
        ),
        DirectiveInterpretation(
            note_index=1, applies=True, directive_type="minimum_battery_reserve",
            structured_adjustment={"hours": [18, 19, 20, 21], "minimum_energy_kwh": 200.0},
            explanation="200 kWh reserve in [18, 22)"
        ),
        DirectiveInterpretation(
            note_index=2, applies=True, directive_type="max_grid_window",
            structured_adjustment={"hours": [18, 19, 20], "max_grid_kwh": 80.0},
            explanation="grid capped at 80 kWh in [18, 21)"
        ),
    ],
    "case-8-mixed-and-noop": [
        DirectiveInterpretation(
            note_index=0, applies=False, directive_type="no_op",
            structured_adjustment=None, explanation="cafeteria is not a directive"
        ),
        DirectiveInterpretation(
            note_index=1, applies=True, directive_type="solar_reduction",
            structured_adjustment={"hours": [10, 11, 12, 13], "factor": 0.25},
            explanation="25% remaining between 10 and 14"
        ),
        DirectiveInterpretation(
            note_index=2, applies=True, directive_type="max_grid_window",
            structured_adjustment={"hours": [19, 20, 21], "max_grid_kwh": 70.0},
            explanation="grid capped at 70 in [19, 22)"
        ),
    ],
    "case-9-paraphrase-heavy": [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="solar_reduction",
            structured_adjustment={"hours": [13, 14], "factor": 0.2},
            explanation="one-fifth of normal solar in [13, 15)"
        ),
        DirectiveInterpretation(
            note_index=1, applies=False, directive_type="no_op",
            structured_adjustment=None, explanation="sports is not a directive"
        ),
    ],
    "case-10-edge-tight-reserves": [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="minimum_battery_reserve",
            structured_adjustment={"hours": [0, 1, 2, 3, 4, 5], "minimum_energy_kwh": 120.0},
            explanation="reserve >= 120 in [0, 6)"
        ),
        DirectiveInterpretation(
            note_index=1, applies=True, directive_type="max_grid_window",
            structured_adjustment={"hours": [18, 19, 20], "max_grid_kwh": 60.0},
            explanation="grid capped at 60 in [18, 21)"
        ),
    ],
}


def _case_id(case: dict) -> str:
    return case.get("id") or case.get("scenario_id")


@pytest.mark.parametrize("case", _load_cases())
def test_public_case_returns_valid_response(case, stub):
    interpretations = _INTERPRETATIONS.get(_case_id(case))
    if interpretations is None:
        # Unknown case id: just no-op every note.
        interpretations = [
            DirectiveInterpretation(
                note_index=i, applies=False, directive_type="no_op",
                structured_adjustment=None, explanation="unknown case fallback",
            )
            for i in range(len(case["operator_notes"]))
        ]
    set_stub_interpretations(stub, list(interpretations))

    # The public cases JSON includes a case-id metadata field that isn't part
    # of the API schema; strip it before posting.
    payload = {k: v for k, v in case.items() if k != "id"}
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Schema checks.
    assert body["scenario_id"] == case["scenario_id"]
    assert len(body["directive_interpretation"]) == len(case["operator_notes"])
    indices = sorted(d["note_index"] for d in body["directive_interpretation"])
    assert indices == list(range(len(case["operator_notes"])))

    # Schedule checks.
    plan = body["hourly_plan"]
    assert len(plan) == 24
    assert sorted(e["hour"] for e in plan) == list(range(24))

    battery_initial = case["battery"]["initial_energy_kwh"]
    last = [e for e in plan if e["hour"] == 23][0]
    assert abs(last["battery_energy_after_kwh"] - battery_initial) < 0.01

    tariff = {h["hour"]: h["tariff_bdt_per_kwh"] for h in case["hours"]}
    for entry in plan:
        hour = entry["hour"]
        src = case["hours"][hour]
        assert entry["grid_kwh"] >= -0.01
        assert entry["solar_used_kwh"] >= -0.01
        if entry["battery_action"] == "idle":
            assert entry["battery_kwh"] == 0
        lhs = entry["grid_kwh"] + entry["solar_used_kwh"] + (
            entry["battery_kwh"] if entry["battery_action"] == "discharge" else 0.0
        )
        rhs = src["demand_kwh"] + (
            entry["battery_kwh"] if entry["battery_action"] == "charge" else 0.0
        )
        assert abs(lhs - rhs) < 0.01

    # Totals are self-consistent.
    total_grid = sum(e["grid_kwh"] for e in plan)
    total_cost = sum(e["grid_kwh"] * tariff[e["hour"]] for e in plan)
    peak = max(e["grid_kwh"] for e in plan)
    assert abs(total_grid - body["total_grid_kwh"]) < 0.01
    assert abs(total_cost - body["total_cost_bdt"]) < 0.01
    assert abs(peak - body["peak_grid_kwh"]) < 0.01

    # Directive constraints.
    for directive in interpretations:
        if directive.directive_type == "no_op":
            continue
        adj = directive.structured_adjustment or {}
        hours = adj.get("hours", [])
        if directive.directive_type == "no_charge_window":
            for h in hours:
                e = next(x for x in plan if x["hour"] == h)
                assert e["battery_action"] != "charge"
        if directive.directive_type == "no_discharge_window":
            for h in hours:
                e = next(x for x in plan if x["hour"] == h)
                assert e["battery_action"] != "discharge"
        if directive.directive_type == "minimum_battery_reserve":
            for h in hours:
                e = next(x for x in plan if x["hour"] == h)
                assert e["battery_energy_after_kwh"] >= adj["minimum_energy_kwh"] - 0.01
        if directive.directive_type == "max_grid_window":
            for h in hours:
                e = next(x for x in plan if x["hour"] == h)
                assert e["grid_kwh"] <= adj["max_grid_kwh"] + 0.01
        if directive.directive_type == "solar_reduction":
            factor = adj["factor"]
            for h in hours:
                e = next(x for x in plan if x["hour"] == h)
                src = case["hours"][h]
                assert e["solar_used_kwh"] <= src["solar_kwh"] * factor + 0.01


def test_public_cases_loaded():
    cases = _load_cases()
    assert len(cases) == 10, "Expected exactly 10 public cases"