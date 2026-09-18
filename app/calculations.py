"""Numeric helpers shared across the directive/optimizer/validator pipeline."""
from __future__ import annotations

from typing import Iterable, List, Sequence

from .schemas import HourlyPlanEntry, HourRecord


def normalize_hour_list(hours: Iterable[int]) -> List[int]:
    """Return a sorted list of unique integer hours in 0..23."""
    out = sorted({int(h) for h in hours if 0 <= int(h) <= 23})
    return out


def effective_solar(solar_kwh: float, factor: float) -> float:
    """Apply a solar reduction factor (clamped to 0..1) to a single hour."""
    f = max(0.0, min(1.0, float(factor)))
    return max(0.0, float(solar_kwh) * f)


def plan_totals(plan: Sequence[HourlyPlanEntry], hours: Sequence[HourRecord]) -> tuple[float, float, float]:
    """Recalculate totals independently from the hourly plan.

    Returns ``(total_grid_kwh, total_cost_bdt, peak_grid_kwh)``.
    """
    tariff_map = {h.hour: h.tariff_bdt_per_kwh for h in hours}
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0
    for entry in plan:
        g = float(entry.grid_kwh)
        total_grid += g
        total_cost += g * float(tariff_map[entry.hour])
        peak_grid = max(peak_grid, g)
    return round(total_grid, 4), round(total_cost, 4), round(peak_grid, 4)


def round_entry(entry: HourlyPlanEntry, decimals: int = 4) -> HourlyPlanEntry:
    """Return a copy of an entry with rounded numeric fields."""
    return HourlyPlanEntry(
        hour=entry.hour,
        grid_kwh=round(float(entry.grid_kwh), decimals),
        solar_used_kwh=round(float(entry.solar_used_kwh), decimals),
        battery_action=entry.battery_action,
        battery_kwh=round(float(entry.battery_kwh), decimals),
        battery_energy_after_kwh=round(float(entry.battery_energy_after_kwh), decimals),
    )


def almost_equal(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(float(a) - float(b)) <= tol
