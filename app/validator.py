"""Independent validator: replays the schedule to verify all constraints."""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from .calculations import almost_equal, effective_solar, normalize_hour_list
from .config import SETTINGS
from .directives import MergedDirectives
from .errors import ValidatorError
from .schemas import BatteryConfig, DirectiveInterpretation, HourlyPlanEntry, HourRecord

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE = 0.01


def _tol() -> float:
    return float(SETTINGS.tolerance)


def _check(condition: bool, message: str, detail: Optional[dict] = None) -> None:
    if not condition:
        raise ValidatorError(message, detail=detail)


class ScheduleValidator:
    """Stateless schedule validator."""

    def __init__(
        self,
        hours: Sequence[HourRecord],
        battery: BatteryConfig,
        directives: MergedDirectives,
    ) -> None:
        self.hours = list(hours)
        self.hour_map: Dict[int, HourRecord] = {h.hour: h for h in self.hours}
        self.battery = battery
        self.directives = directives
        self.tol = _tol()

    # ---- public API ----

    def validate_plan(self, plan: Sequence[HourlyPlanEntry]) -> None:
        self._check_basics(plan)
        self._check_per_hour(plan)
        self._check_directives(plan)
        self._check_neutrality(plan)
        self._check_totals(plan)

    def compute_expected_schedule(self, plan: Sequence[HourlyPlanEntry]) -> List[HourlyPlanEntry]:
        """Replay the plan deterministically to validate internal consistency.

        This is informational / used by tests; the schedule returned by the
        optimizer is canonical for the API response.
        """
        replay: List[HourlyPlanEntry] = []
        prev_energy = float(self.battery.initial_energy_kwh)
        for entry in plan:
            action = entry.battery_action
            bkw = float(entry.battery_kwh)
            if action == "charge":
                new_energy = prev_energy + bkw
            elif action == "discharge":
                new_energy = prev_energy - bkw
            else:
                new_energy = prev_energy
            replay.append(
                HourlyPlanEntry(
                    hour=entry.hour,
                    grid_kwh=float(entry.grid_kwh),
                    solar_used_kwh=float(entry.solar_used_kwh),
                    battery_action=action,
                    battery_kwh=bkw,
                    battery_energy_after_kwh=new_energy,
                )
            )
            prev_energy = new_energy
        return replay

    # ---- check methods ----

    def _check_basics(self, plan: Sequence[HourlyPlanEntry]) -> None:
        _check(len(plan) == 24, "Plan must contain exactly 24 entries", {"got": len(plan)})
        seen = set()
        for entry in plan:
            _check(entry.hour in range(24), "Hour out of range", {"hour": entry.hour})
            _check(entry.hour not in seen, "Duplicate hour in plan", {"hour": entry.hour})
            seen.add(entry.hour)
        _check(seen == set(range(24)), "Plan must cover hours 0..23", {"missing": sorted(set(range(24)) - seen)})

    def _check_per_hour(self, plan: Sequence[HourlyPlanEntry]) -> None:
        cap = float(self.battery.capacity_kwh)
        base_min = float(self.battery.minimum_energy_kwh)
        max_charge = float(self.battery.max_charge_kwh_per_hour)
        max_discharge = float(self.battery.max_discharge_kwh_per_hour)
        prev_energy = float(self.battery.initial_energy_kwh)

        for entry in plan:
            hour = entry.hour
            src = self.hour_map[hour]
            demand = float(src.demand_kwh)
            solar_avail = effective_solar(
                float(src.solar_kwh), self.directives.effective_solar_factor(hour)
            )

            _check(entry.grid_kwh >= -self.tol, "grid_kwh must be >= 0", {"hour": hour})
            _check(entry.solar_used_kwh >= -self.tol, "solar_used_kwh must be >= 0", {"hour": hour})
            _check(
                entry.solar_used_kwh <= solar_avail + self.tol,
                "solar_used exceeds effective solar",
                {"hour": hour, "used": entry.solar_used_kwh, "eff": solar_avail},
            )

            _check(
                entry.battery_action in {"charge", "discharge", "idle"},
                "battery_action invalid",
                {"hour": hour, "action": entry.battery_action},
            )
            _check(entry.battery_kwh >= -self.tol, "battery_kwh must be >= 0", {"hour": hour})

            # apply transition
            if entry.battery_action == "charge":
                new_energy = prev_energy + float(entry.battery_kwh)
                _check(
                    float(entry.battery_kwh) <= max_charge + self.tol,
                    "charge rate exceeded",
                    {"hour": hour, "kwh": entry.battery_kwh, "max": max_charge},
                )
            elif entry.battery_action == "discharge":
                new_energy = prev_energy - float(entry.battery_kwh)
                _check(
                    float(entry.battery_kwh) <= max_discharge + self.tol,
                    "discharge rate exceeded",
                    {"hour": hour, "kwh": entry.battery_kwh, "max": max_discharge},
                )
            else:
                _check(almost_equal(entry.battery_kwh, 0.0, self.tol), "idle action must have kwh=0")
                new_energy = prev_energy

            _check(
                almost_equal(float(entry.battery_energy_after_kwh), new_energy, self.tol),
                "battery_energy_after does not match transition",
                {"hour": hour, "report": entry.battery_energy_after_kwh, "expected": new_energy},
            )

            _check(
                new_energy <= cap + self.tol,
                "battery over capacity",
                {"hour": hour, "energy": new_energy, "cap": cap},
            )
            _check(
                new_energy >= base_min - self.tol,
                "battery below base reserve",
                {"hour": hour, "energy": new_energy, "base_min": base_min},
            )

            # Energy balance.
            lhs = (
                float(entry.grid_kwh)
                + float(entry.solar_used_kwh)
                + (float(entry.battery_kwh) if entry.battery_action == "discharge" else 0.0)
            )
            rhs = (
                demand
                + (float(entry.battery_kwh) if entry.battery_action == "charge" else 0.0)
            )
            _check(
                almost_equal(lhs, rhs, self.tol),
                "energy balance failed",
                {"hour": hour, "lhs": lhs, "rhs": rhs},
            )

            prev_energy = new_energy

    def _check_directives(self, plan: Sequence[HourlyPlanEntry]) -> None:
        # no_charge / no_discharge.
        for h in self.directives.no_charge_hours:
            entry = next(e for e in plan if e.hour == h)
            _check(entry.battery_action != "charge", "charge detected in no_charge_window", {"hour": h})
        for h in self.directives.no_discharge_hours:
            entry = next(e for e in plan if e.hour == h)
            _check(entry.battery_action != "discharge", "discharge detected in no_discharge_window", {"hour": h})
        # reserve
        for h, reserve in self.directives.battery_reserve.items():
            entry = next(e for e in plan if e.hour == h)
            _check(
                entry.battery_energy_after_kwh >= reserve - self.tol,
                "battery below directive reserve",
                {"hour": h, "energy": entry.battery_energy_after_kwh, "reserve": reserve},
            )
        # max grid
        for h, cap in self.directives.max_grid_kwh.items():
            entry = next(e for e in plan if e.hour == h)
            _check(
                entry.grid_kwh <= cap + self.tol,
                "grid import exceeded directive cap",
                {"hour": h, "grid": entry.grid_kwh, "cap": cap},
            )

    def _check_neutrality(self, plan: Sequence[HourlyPlanEntry]) -> None:
        last = next(e for e in plan if e.hour == 23)
        _check(
            almost_equal(last.battery_energy_after_kwh, float(self.battery.initial_energy_kwh), self.tol),
            "end-of-day battery must equal initial energy",
            {
                "report": last.battery_energy_after_kwh,
                "expected": self.battery.initial_energy_kwh,
            },
        )

    def _check_totals(self, plan: Sequence[HourlyPlanEntry]) -> None:
        total_grid = sum(float(e.grid_kwh) for e in plan)
        total_cost = sum(float(e.grid_kwh) * float(self.hour_map[e.hour].tariff_bdt_per_kwh) for e in plan)
        peak_grid = max(float(e.grid_kwh) for e in plan)
        _check(total_grid > -self.tol, "negative total grid", {"total_grid": total_grid})
        _check(total_cost > -self.tol, "negative total cost", {"total_cost": total_cost})
        _check(peak_grid >= -self.tol, "negative peak", {"peak": peak_grid})


def validate_interpretations_against_notes(
    interpretations: List[DirectiveInterpretation],
    notes: List[str],
) -> None:
    _check(len(interpretations) == len(notes), "interpretations must cover every note")
    indices = sorted(i.note_index for i in interpretations)
    _check(indices == list(range(len(notes))), "note_index values must be 0..N-1")
    for i in interpretations:
        if i.directive_type == "no_op":
            _check(i.applies is False, "no_op applies must be false")
            _check(i.structured_adjustment is None, "no_op structured_adjustment must be null")
        else:
            _check(i.applies is True, "non-no_op applies must be true")
            _check(i.structured_adjustment is not None, "non-no_op requires structured_adjustment")
            _check("hours" in i.structured_adjustment, "adjustment missing hours")
            hours = normalize_hour_list(i.structured_adjustment["hours"])
            if i.directive_type == "solar_reduction":
                f = float(i.structured_adjustment["factor"])
                _check(0 <= f <= 1, "solar factor out of range")
            if i.directive_type == "minimum_battery_reserve":
                _check(i.structured_adjustment["minimum_energy_kwh"] >= 0, "reserve must be non-negative")
            if i.directive_type == "max_grid_window":
                _check(i.structured_adjustment["max_grid_kwh"] >= 0, "grid cap must be non-negative")


def validate_final_response(
    plan: Sequence[HourlyPlanEntry],
    hours: Sequence[HourRecord],
    battery: BatteryConfig,
    directives: MergedDirectives,
    interpretations: List[DirectiveInterpretation],
    notes: List[str],
    reported_totals: Dict[str, float],
) -> None:
    """End-to-end verification of the optimizer output before it ships."""
    validate_interpretations_against_notes(interpretations, notes)

    validator = ScheduleValidator(hours, battery, directives)
    validator.validate_plan(plan)

    # Independent totals.
    total_grid = sum(float(e.grid_kwh) for e in plan)
    total_cost = sum(
        float(e.grid_kwh) * float(validator.hour_map[e.hour].tariff_bdt_per_kwh) for e in plan
    )
    peak = max(float(e.grid_kwh) for e in plan)

    _check(
        almost_equal(total_grid, float(reported_totals["total_grid_kwh"]), validator.tol),
        "reported total_grid_kwh does not match plan",
        {"reported": reported_totals["total_grid_kwh"], "computed": total_grid},
    )
    _check(
        almost_equal(total_cost, float(reported_totals["total_cost_bdt"]), validator.tol),
        "reported total_cost_bdt does not match plan",
        {"reported": reported_totals["total_cost_bdt"], "computed": total_cost},
    )
    _check(
        almost_equal(peak, float(reported_totals["peak_grid_kwh"]), validator.tol),
        "reported peak_grid_kwh does not match plan",
        {"reported": reported_totals["peak_grid_kwh"], "computed": peak},
    )
