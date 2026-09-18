"""Optimizer behaviour tests covering all directives and combinations."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.directives import MergedDirectives, merge_directives
from app.main import app
from app.optimizer import optimize_schedule
from app.schemas import DirectiveInterpretation
from tests._helpers import (
    baseline_battery,
    baseline_hours,
    request_payload,
    set_stub_interpretations,
)


client = TestClient(app)


def _interp(idx, dtype, adj, applies=True, explanation=""):
    return DirectiveInterpretation(
        note_index=idx, applies=applies, directive_type=dtype,
        structured_adjustment=adj, explanation=explanation,
    )


def _post(notes, interpretations):
    set_stub_interpretations(stub_global=None, items=interpretations)  # placeholders
    # Actually we need the fixture; rewrite directly using a fresh stub:
    raise RuntimeError("unused; tests below call set_stub_interpretations via fixture")


def test_solar_reduction_affects_only_target_hours(stub):
    set_stub_interpretations(stub, [
        _interp(0, "solar_reduction", {"hours": [13, 14], "factor": 0.2})
    ])
    resp = client.post("/optimize-energy", json=request_payload(["PV drop"]))
    body = resp.json()
    hourly = {e["hour"]: e for e in body["hourly_plan"]}
    # In hours 13/14, solar should be capped at ~20% of 160/140.
    assert hourly[13]["solar_used_kwh"] <= 160 * 0.2 + 0.01
    assert hourly[14]["solar_used_kwh"] <= 140 * 0.2 + 0.01
    # In hour 9 (untouched), solar can be used up to 110.
    assert hourly[9]["solar_used_kwh"] <= 110 + 0.01


def test_no_charge_window_blocks_charging(stub):
    set_stub_interpretations(stub, [
        _interp(0, "no_charge_window", {"hours": [9, 10, 11, 12, 13]})
    ])
    resp = client.post("/optimize-energy", json=request_payload(["no charge midday"]))
    body = resp.json()
    hourly = {e["hour"]: e for e in body["hourly_plan"]}
    for h in [9, 10, 11, 12, 13]:
        assert hourly[h]["battery_action"] != "charge"
        assert hourly[h]["battery_kwh"] == 0 or hourly[h]["battery_action"] != "charge"


def test_no_discharge_window_blocks_discharging(stub):
    set_stub_interpretations(stub, [
        _interp(0, "no_discharge_window", {"hours": [18, 19, 20]})
    ])
    resp = client.post("/optimize-energy", json=request_payload(["no discharge evening"]))
    body = resp.json()
    hourly = {e["hour"]: e for e in body["hourly_plan"]}
    for h in [18, 19, 20]:
        assert hourly[h]["battery_action"] != "discharge"


def test_minimum_battery_reserve(stub):
    set_stub_interpretations(stub, [
        _interp(0, "minimum_battery_reserve", {"hours": [18, 19, 20], "minimum_energy_kwh": 300})
    ])
    resp = client.post("/optimize-energy", json=request_payload(["reserve"]))
    body = resp.json()
    hourly = {e["hour"]: e for e in body["hourly_plan"]}
    for h in [18, 19, 20]:
        assert hourly[h]["battery_energy_after_kwh"] >= 300 - 0.01


def test_max_grid_window_caps_grid(stub):
    set_stub_interpretations(stub, [
        _interp(0, "max_grid_window", {"hours": [12, 13, 14], "max_grid_kwh": 50.0})
    ])
    resp = client.post("/optimize-energy", json=request_payload(["grid cap"]))
    body = resp.json()
    hourly = {e["hour"]: e for e in body["hourly_plan"]}
    for h in [12, 13, 14]:
        assert hourly[h]["grid_kwh"] <= 50.0 + 0.01


def test_no_op_keeps_normal_optimization(stub):
    set_stub_interpretations(stub, [
        _interp(0, "no_op", None, applies=False)
    ])
    resp = client.post("/optimize-energy", json=request_payload(["irrelevant"]))
    body = resp.json()
    assert resp.status_code == 200
    assert body["plan_summary"].startswith("GridWise schedule applies no active operator directives;")


def test_combined_directives(stub):
    """Three directives together: solar reduction, reserve, grid cap."""
    set_stub_interpretations(stub, [
        _interp(0, "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
        _interp(1, "minimum_battery_reserve", {"hours": [18, 19, 20], "minimum_energy_kwh": 200.0}),
        _interp(2, "max_grid_window", {"hours": [18, 19], "max_grid_kwh": 80.0}),
    ])
    resp = client.post("/optimize-energy", json=request_payload(["solar drop", "reserve 200kWh", "grid cap"]))
    body = resp.json()
    hourly = {e["hour"]: e for e in body["hourly_plan"]}
    assert hourly[13]["solar_used_kwh"] <= 32 + 0.01  # 160 * 0.2
    for h in [18, 19, 20]:
        assert hourly[h]["battery_energy_after_kwh"] >= 200 - 0.01
    for h in [18, 19]:
        assert hourly[h]["grid_kwh"] <= 80 + 0.01


def test_end_of_day_neutrality(stub):
    set_stub_interpretations(stub, [
        _interp(0, "solar_reduction", {"hours": [13, 14], "factor": 0.5})
    ])
    resp = client.post("/optimize-energy", json=request_payload(["any note"]))
    body = resp.json()
    last = [e for e in body["hourly_plan"] if e["hour"] == 23][0]
    assert abs(last["battery_energy_after_kwh"] - 200) < 0.01


def test_optimizer_produces_full_24_hours(stub):
    resp = client.post("/optimize-energy", json=request_payload(["note"]))
    body = resp.json()
    assert len(body["hourly_plan"]) == 24
    hours_seen = sorted(e["hour"] for e in body["hourly_plan"])
    assert hours_seen == list(range(24))


def test_optimizer_pure_function_no_sideeffects():
    """Calling optimize_schedule directly without an HTTP layer."""
    merged = merge_directives([
        _interp(0, "solar_reduction", {"hours": [13, 14], "factor": 0.2})
    ])
    hours = baseline_hours()
    battery = baseline_battery()
    from app.schemas import HourRecord, BatteryConfig
    hours_models = [HourRecord(**h) for h in hours]
    battery_model = BatteryConfig(**battery)
    plan = optimize_schedule(hours_models, battery_model, merged)
    assert len(plan) == 24


def test_grid_kwh_never_negative(stub):
    set_stub_interpretations(stub, [
        _interp(0, "max_grid_window", {"hours": [0, 1, 2, 3, 4], "max_grid_kwh": 0.0})
    ])
    # Use a battery with enough initial energy to cover the night demand
    # (335 kWh across hours 0-4) without dipping below the safety reserve.
    big_battery = {
        "capacity_kwh": 500.0,
        "initial_energy_kwh": 400.0,
        "minimum_energy_kwh": 50.0,
        "max_charge_kwh_per_hour": 100.0,
        "max_discharge_kwh_per_hour": 100.0,
    }
    resp = client.post(
        "/optimize-energy",
        json=request_payload(["grid cap night"], battery=big_battery),
    )
    body = resp.json()
    for e in body["hourly_plan"]:
        assert e["grid_kwh"] >= -0.0001


def test_battery_action_must_be_known(stub):
    resp = client.post("/optimize-energy", json=request_payload(["note"]))
    body = resp.json()
    for e in body["hourly_plan"]:
        assert e["battery_action"] in ("charge", "discharge", "idle")


def test_idle_action_has_zero_kwh(stub):
    resp = client.post("/optimize-energy", json=request_payload(["note"]))
    body = resp.json()
    for e in body["hourly_plan"]:
        if e["battery_action"] == "idle":
            assert e["battery_kwh"] == 0