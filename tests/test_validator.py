"""Validator replay tests."""
from __future__ import annotations

import pytest

from app.directives import MergedDirectives
from app.errors import ValidatorError
from app.schemas import BatteryConfig, DirectiveInterpretation, HourRecord, HourlyPlanEntry
from app.validator import ScheduleValidator, validate_final_response


def _make_hours():
    return [
        HourRecord(hour=h, demand_kwh=100, solar_kwh=20, tariff_bdt_per_kwh=6)
        for h in range(24)
    ]


def _make_battery(initial=50):
    return BatteryConfig(
        capacity_kwh=200,
        initial_energy_kwh=initial,
        minimum_energy_kwh=20,
        max_charge_kwh_per_hour=50,
        max_discharge_kwh_per_hour=50,
    )


def _make_plan(grid_per_hour, solar_per_hour, b_actions, b_kwh, b_after):
    return [
        HourlyPlanEntry(
            hour=h,
            grid_kwh=grid_per_hour[h],
            solar_used_kwh=solar_per_hour[h],
            battery_action=b_actions[h],
            battery_kwh=b_kwh[h],
            battery_energy_after_kwh=b_after[h],
        )
        for h in range(24)
    ]


def test_validator_accepts_balanced_plan():
    hours = _make_hours()
    battery = _make_battery(80)
    # Manually construct a valid day: charge in cheap hours, discharge in expensive ones.
    grid = [20] * 24
    solar = [20] * 24
    actions = ["idle"] * 24
    kwh = [0.0] * 24
    after = [80.0] * 24
    # To keep plan valid, follow balance: grid + solar + discharge = demand + charge
    # demand=100, solar=20, so grid must be 100-20=80 if idle.
    grid = [80.0] * 24
    plan = _make_plan(grid, solar, actions, kwh, after)
    v = ScheduleValidator(hours, battery, MergedDirectives())
    v.validate_plan(plan)


def test_validator_rejects_bad_grid_sign():
    hours = _make_hours()
    battery = _make_battery(80)
    plan = _make_plan([80.0] * 24, [20.0] * 24, ["idle"] * 24, [0.0] * 24, [80.0] * 24)
    # Bypass Pydantic's ge=0 check on grid_kwh so we can verify the runtime
    # validator's own guard.  ``model_construct`` skips validation.
    plan[5] = HourlyPlanEntry.model_construct(
        hour=5, grid_kwh=-1, solar_used_kwh=20, battery_action="idle",
        battery_kwh=0, battery_energy_after_kwh=80,
    )
    v = ScheduleValidator(hours, battery, MergedDirectives())
    with pytest.raises(ValidatorError):
        v.validate_plan(plan)


def test_validator_rejects_end_of_day_mismatch():
    hours = _make_hours()
    battery = _make_battery(80)
    plan = _make_plan([80.0] * 24, [20.0] * 24, ["idle"] * 24, [0.0] * 24, [80.0] * 24)
    plan[23] = HourlyPlanEntry(hour=23, grid_kwh=80, solar_used_kwh=20, battery_action="idle", battery_kwh=0, battery_energy_after_kwh=70)
    v = ScheduleValidator(hours, battery, MergedDirectives())
    with pytest.raises(ValidatorError):
        v.validate_plan(plan)


def test_validator_rejects_reserve_violation():
    hours = _make_hours()
    battery = _make_battery(80)
    merged = MergedDirectives()
    merged.battery_reserve[12] = 150
    plan = _make_plan([80.0] * 24, [20.0] * 24, ["idle"] * 24, [0.0] * 24, [80.0] * 24)
    v = ScheduleValidator(hours, battery, merged)
    with pytest.raises(ValidatorError):
        v.validate_plan(plan)


def test_validator_rejects_grid_cap_violation():
    hours = _make_hours()
    battery = _make_battery(80)
    merged = MergedDirectives()
    merged.max_grid_kwh[0] = 50
    plan = _make_plan([80.0] * 24, [20.0] * 24, ["idle"] * 24, [0.0] * 24, [80.0] * 24)
    v = ScheduleValidator(hours, battery, merged)
    with pytest.raises(ValidatorError):
        v.validate_plan(plan)


def test_validator_rejects_idle_with_nonzero_kwh():
    hours = _make_hours()
    battery = _make_battery(80)
    plan = _make_plan([80.0] * 24, [20.0] * 24, ["idle"] * 24, [0.0] * 24, [80.0] * 24)
    plan[10] = HourlyPlanEntry(hour=10, grid_kwh=60, solar_used_kwh=20, battery_action="idle", battery_kwh=20, battery_energy_after_kwh=80)
    v = ScheduleValidator(hours, battery, MergedDirectives())
    with pytest.raises(ValidatorError):
        v.validate_plan(plan)


def test_validate_final_response_checks_totals():
    hours = _make_hours()
    battery = _make_battery(80)
    plan = _make_plan([80.0] * 24, [20.0] * 24, ["idle"] * 24, [0.0] * 24, [80.0] * 24)
    reported = {"total_grid_kwh": 80 * 24, "total_cost_bdt": 80 * 24 * 6, "peak_grid_kwh": 80}
    interp = DirectiveInterpretation(
        note_index=0, applies=False, directive_type="no_op",
        structured_adjustment=None, explanation="placeholder",
    )
    validate_final_response(
        plan=plan, hours=hours, battery=battery, directives=MergedDirectives(),
        interpretations=[interp], notes=["x"],
        reported_totals=reported,
    )


def test_validate_final_response_catches_mismatch():
    hours = _make_hours()
    battery = _make_battery(80)
    plan = _make_plan([80.0] * 24, [20.0] * 24, ["idle"] * 24, [0.0] * 24, [80.0] * 24)
    reported = {"total_grid_kwh": 1234, "total_cost_bdt": 0, "peak_grid_kwh": 0}
    interp = DirectiveInterpretation(
        note_index=0, applies=False, directive_type="no_op",
        structured_adjustment=None, explanation="placeholder",
    )
    with pytest.raises(ValidatorError):
        validate_final_response(
            plan=plan, hours=hours, battery=battery, directives=MergedDirectives(),
            interpretations=[interp], notes=["x"], reported_totals=reported,
        )