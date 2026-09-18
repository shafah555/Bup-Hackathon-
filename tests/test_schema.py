"""Schema and request validation tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas import BatteryConfig, HourRecord, OptimizeRequest
from tests._helpers import baseline_battery, baseline_hours, request_payload


client = TestClient(app)


def _post(payload):
    return client.post("/optimize-energy", json=payload)


def test_valid_request_passes_pydantic():
    payload = request_payload(["Use as much solar as possible."])
    req = OptimizeRequest(**payload)
    assert req.scenario_id == "scenario-test"
    assert len(req.hours) == 24


def test_missing_scenario_id_rejected():
    payload = request_payload(["Note"])
    del payload["scenario_id"]
    resp = _post(payload)
    assert resp.status_code == 422


def test_empty_notes_rejected():
    payload = request_payload([])
    with pytest.raises(ValidationError):
        OptimizeRequest(**payload)


def test_notes_too_many_rejected():
    payload = request_payload(["a", "b", "c", "d"])
    with pytest.raises(ValidationError):
        OptimizeRequest(**payload)


def test_only_24_hours_allowed():
    payload = request_payload(["note"])
    payload["hours"] = payload["hours"][:23]
    with pytest.raises(ValidationError):
        OptimizeRequest(**payload)


def test_duplicate_hours_rejected():
    hours = baseline_hours()
    hours[0]["hour"] = 1
    payload = request_payload(["note"], hours=hours)
    with pytest.raises(ValidationError):
        OptimizeRequest(**payload)


def test_negative_demand_rejected():
    hours = baseline_hours()
    hours[0]["demand_kwh"] = -10
    payload = request_payload(["note"], hours=hours)
    with pytest.raises(ValidationError):
        OptimizeRequest(**payload)


def test_battery_capacity_invariants():
    bad = baseline_battery()
    bad["initial_energy_kwh"] = 600  # > capacity
    with pytest.raises(ValidationError):
        BatteryConfig(**bad)


def test_battery_initial_below_minimum_rejected():
    bad = baseline_battery()
    bad["initial_energy_kwh"] = 10
    bad["minimum_energy_kwh"] = 50
    with pytest.raises(ValidationError):
        BatteryConfig(**bad)


def test_api_returns_422_on_bad_payload():
    bad = request_payload(["note"])
    bad["hours"][0]["demand_kwh"] = -5
    resp = _post(bad)
    assert resp.status_code == 422


def test_hour_field_range_enforced():
    hours = baseline_hours()
    hours[0] = {**hours[0], "hour": 24}
    payload = request_payload(["note"], hours=hours)
    with pytest.raises(ValidationError):
        OptimizeRequest(**payload)