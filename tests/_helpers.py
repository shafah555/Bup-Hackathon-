"""Reusable test payload builders."""
from __future__ import annotations

from typing import Any, Dict, List


def _hour(hour: int, demand: float, solar: float, tariff: float) -> Dict[str, float]:
    return {
        "hour": hour,
        "demand_kwh": demand,
        "solar_kwh": solar,
        "tariff_bdt_per_kwh": tariff,
    }


def baseline_hours() -> List[Dict[str, float]]:
    """24-hour synthetic campus load."""
    base_demand = [
        80, 70, 65, 60, 60, 65, 90, 130,
        160, 170, 180, 190, 200, 200, 200, 200,
        190, 180, 170, 170, 160, 140, 110, 80,
    ]
    base_solar = [
        0, 0, 0, 0, 0, 5, 20, 50,
        80, 110, 140, 160, 170, 160, 140, 110,
        80, 40, 10, 0, 0, 0, 0, 0,
    ]
    base_tariff = [
        6, 6, 6, 6, 6, 6, 6, 7,
        7, 7, 8, 8, 10, 12, 14, 14,
        12, 12, 10, 10, 9, 9, 8, 7,
    ]
    return [
        _hour(h, base_demand[h], base_solar[h], base_tariff[h])
        for h in range(24)
    ]


def baseline_battery() -> Dict[str, float]:
    return {
        "capacity_kwh": 500,
        "initial_energy_kwh": 200,
        "minimum_energy_kwh": 50,
        "max_charge_kwh_per_hour": 100,
        "max_discharge_kwh_per_hour": 100,
    }


def request_payload(
    notes: List[str],
    *,
    hours: List[Dict[str, float]] = None,
    battery: Dict[str, float] = None,
    scenario_id: str = "scenario-test",
) -> Dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "operator_notes": notes,
        "hours": hours if hours is not None else baseline_hours(),
        "battery": battery if battery is not None else baseline_battery(),
    }


def set_stub_interpretations(stub, items: List) -> None:
    """Queue a single response on the stub interpreter."""
    stub.interpretations.append(items)
