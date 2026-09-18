"""Merge 1..3 directive interpretations into a unified constraint set."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .calculations import normalize_hour_list
from .schemas import DirectiveInterpretation


@dataclass
class MergedDirectives:
    """Normalized representation of all directives for the optimizer."""

    solar_factor: Dict[int, float] = field(default_factory=dict)
    battery_reserve: Dict[int, float] = field(default_factory=dict)
    no_charge_hours: Set[int] = field(default_factory=set)
    no_discharge_hours: Set[int] = field(default_factory=set)
    max_grid_kwh: Dict[int, float] = field(default_factory=dict)
    active_directive_types: Set[str] = field(default_factory=set)

    def effective_solar_factor(self, hour: int) -> float:
        return self.solar_factor.get(hour, 1.0)

    def effective_reserve(self, hour: int, base_min: float) -> float:
        v = self.battery_reserve.get(hour)
        if v is None:
            return base_min
        return max(base_min, v)

    def effective_max_grid(self, hour: int) -> Optional[float]:
        return self.max_grid_kwh.get(hour)


def merge_directives(interpretations: List[DirectiveInterpretation]) -> MergedDirectives:
    """Merge all non-no_op directive interpretations into one MergedDirectives.

    Notes:
        - For ``solar_reduction`` we keep the *minimum* factor per hour (most
          restrictive). The remaining fraction can only decrease when multiple
          reductions apply.
        - For ``minimum_battery_reserve`` we keep the *maximum* reserve per hour
          (most restrictive). Reserves can only be raised.
        - For ``max_grid_window`` we keep the *minimum* cap per hour.
        - For ``no_charge_window`` and ``no_discharge_window`` we accumulate all
          hours.
    """
    merged = MergedDirectives()

    for interp in interpretations:
        if not interp.applies:
            continue
        dtype = interp.directive_type
        adj = interp.structured_adjustment or {}
        hours = normalize_hour_list(adj.get("hours", []))
        merged.active_directive_types.add(dtype)

        if dtype == "solar_reduction":
            factor = float(adj.get("factor", 1.0))
            for h in hours:
                merged.solar_factor[h] = min(merged.solar_factor.get(h, 1.0), max(0.0, factor))
        elif dtype == "minimum_battery_reserve":
            reserve = float(adj.get("minimum_energy_kwh", 0.0))
            for h in hours:
                merged.battery_reserve[h] = max(merged.battery_reserve.get(h, 0.0), reserve)
        elif dtype == "no_charge_window":
            merged.no_charge_hours.update(hours)
        elif dtype == "no_discharge_window":
            merged.no_discharge_hours.update(hours)
        elif dtype == "max_grid_window":
            cap = float(adj.get("max_grid_kwh", 0.0))
            for h in hours:
                merged.max_grid_kwh[h] = min(merged.max_grid_kwh.get(h, float("inf")), cap)
        elif dtype == "no_op":
            # Nothing to merge.
            pass
        else:
            # Unsupported directive already rejected by guardrails; skip defensively.
            continue

    return merged


def describe_merged_directives(merged: MergedDirectives) -> str:
    parts: List[str] = []
    if merged.solar_factor:
        parts.append("solar reduction in hours " + _h(merged.solar_factor.keys()))
    if merged.battery_reserve:
        parts.append("minimum battery reserve in hours " + _h(merged.battery_reserve.keys()))
    if merged.no_charge_hours:
        parts.append("no-charge in hours " + _h(merged.no_charge_hours))
    if merged.no_discharge_hours:
        parts.append("no-discharge in hours " + _h(merged.no_discharge_hours))
    if merged.max_grid_kwh:
        parts.append("grid cap in hours " + _h(merged.max_grid_kwh.keys()))
    if not parts:
        return "no active operator directives;"
    return "; ".join(parts) + ";"


def _h(hours) -> str:
    return "[" + ", ".join(str(int(h)) for h in sorted(hours)) + "]"
