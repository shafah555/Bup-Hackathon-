"""Linear-programming optimizer for 24-hour energy schedule."""
from __future__ import annotations

import logging
from typing import List

from .calculations import effective_solar
from .config import SETTINGS
from .directives import MergedDirectives
from .errors import OptimizerError
from .schemas import BatteryConfig, HourlyPlanEntry, HourRecord

logger = logging.getLogger(__name__)


def _safe_solver_import():
    try:
        import pulp  # type: ignore
        return pulp
    except Exception as exc:  # pragma: no cover - dependency missing
        raise OptimizerError(
            "PuLP solver is not available",
            detail={"hint": "pip install pulp"},
        ) from exc


def _solve_with_pulp(pulp, pb, time_limit: float):
    """Solve a PuLP model with the bundled CBC solver.

    PuLP's CBC solver supports binary variables, which we use to enforce
    charge/discharge mutual exclusivity per hour.  Status 1 means ``Optimal``.
    """
    solver = pulp.PULP_CBC_CMD(
        timeLimit=max(1.0, float(time_limit)),
        msg=False,
        gapRel=1e-7,
    )
    status = pb.solve(solver)
    return status


def optimize_schedule(
    hours: List[HourRecord],
    battery: BatteryConfig,
    directives: MergedDirectives,
) -> List[HourlyPlanEntry]:
    """Solve the 24-hour energy dispatch as a continuous LP.

    Decision variables per hour h:
        grid[h]    >= 0          grid import
        solar[h]   >= 0          solar used (<= effective solar)
        charge[h]  >= 0          battery charge
        discharge[h]>= 0         battery discharge
        b_e[h]     free          battery state of energy after hour h
    """
    if len(hours) != 24:
        raise OptimizerError("Hours list must contain exactly 24 records")

    sorted_hours = sorted(hours, key=lambda h: h.hour)
    hour_index = {h.hour: i for i, h in enumerate(sorted_hours)}

    pulp = _safe_solver_import()
    pb = pulp.LpProblem("gridwise_24h", pulp.LpMinimize)

    grid = [pulp.LpVariable(f"grid_{i}", lowBound=0) for i in range(24)]
    solar_used = [pulp.LpVariable(f"solar_{i}", lowBound=0) for i in range(24)]
    charge = [pulp.LpVariable(f"charge_{i}", lowBound=0) for i in range(24)]
    discharge = [pulp.LpVariable(f"discharge_{i}", lowBound=0) for i in range(24)]
    energy = [pulp.LpVariable(f"battery_energy_{i}", lowBound=0) for i in range(24)]
    # Binary indicator: 1 if hour is in a charging mode. Forces mutual exclusivity
    # of charge and discharge in an LP-friendly way (CBC handles 24 binaries easily).
    is_charging = [
        pulp.LpVariable(f"is_charging_{i}", lowBound=0, upBound=1, cat=pulp.LpBinary)
        for i in range(24)
    ]

    # Objective: total cost.
    pb += pulp.lpSum(grid[i] * float(sorted_hours[i].tariff_bdt_per_kwh) for i in range(24))

    cap = float(battery.capacity_kwh)
    base_min = float(battery.minimum_energy_kwh)
    max_charge = float(battery.max_charge_kwh_per_hour)
    max_discharge = float(battery.max_discharge_kwh_per_hour)
    initial = float(battery.initial_energy_kwh)

    # Battery transitions and bounds.
    pb += energy[0] == initial + charge[0] - discharge[0], "init_transition"
    for i in range(1, 24):
        pb += energy[i] == energy[i - 1] + charge[i] - discharge[i], f"trans_{i}"

    # End-of-day neutrality.
    pb += energy[23] == initial, "end_of_day"

    # Capacity / base reserve.
    for i in range(24):
        pb += energy[i] <= cap, f"cap_{i}"
        pb += energy[i] >= base_min, f"min_reserve_{i}"

    # Charge / discharge rate limits.
    for i in range(24):
        pb += charge[i] <= max_charge, f"ch_rate_{i}"
        pb += discharge[i] <= max_discharge, f"dis_rate_{i}"

    # Energy balance per hour.
    for i, h in enumerate(sorted_hours):
        demand = float(h.demand_kwh)
        eff_solar = effective_solar(float(h.solar_kwh), directives.effective_solar_factor(h.hour))
        pb += grid[i] + solar_used[i] + discharge[i] == demand + charge[i], f"balance_{i}"
        pb += solar_used[i] <= eff_solar, f"solar_cap_{i}"

    # Directive: no charge / no discharge windows.
    for h in directives.no_charge_hours:
        i = hour_index[h]
        pb += charge[i] == 0, f"no_charge_{i}"
    for h in directives.no_discharge_hours:
        i = hour_index[h]
        pb += discharge[i] == 0, f"no_discharge_{i}"

    # Directive: minimum battery reserve.
    for h, reserve in directives.battery_reserve.items():
        i = hour_index[h]
        pb += energy[i] >= float(reserve), f"dir_reserve_{i}"

    # Directive: max grid window.
    for h, cap_val in directives.max_grid_kwh.items():
        i = hour_index[h]
        pb += grid[i] <= float(cap_val), f"grid_cap_{i}"

    # Mutual exclusivity helper: charge and discharge cannot both be > 0 simultaneously.
    # Modeled with a binary indicator per hour (1 == charging, 0 == discharging/idle)
    # so charging-only and discharging-only are enforced in the LP relaxation via big-M.
    M_charge = max_charge if max_charge > 0 else 0
    M_discharge = max_discharge if max_discharge > 0 else 0
    for i in range(24):
        pb += charge[i] <= M_charge * is_charging[i], f"mutex_ch_{i}"
        pb += discharge[i] <= M_discharge * (1 - is_charging[i]), f"mutex_dis_{i}"

    status = _solve_with_pulp(pulp, pb, SETTINGS.solver_time_limit_seconds)
    if status != 1:
        logger.error("optimizer_failed", extra={"status": status})
        raise OptimizerError(
            "No valid schedule could be produced",
            detail={"solver_status": int(status)},
        )

    plan: List[HourlyPlanEntry] = []
    eps = 1e-3  # threshold to label idle vs charge/discharge
    for i, h in enumerate(sorted_hours):
        g = float(grid[i].value() or 0.0)
        s = float(solar_used[i].value() or 0.0)
        c = float(charge[i].value() or 0.0)
        d = float(discharge[i].value() or 0.0)
        e_after = float(energy[i].value() or 0.0)
        # Prefer the binary indicator when it's confident (avoids floating-point
        # edge cases where both c and d round to 0).
        ic_val = is_charging[i].value()
        ic_int = int(round(ic_val)) if ic_val is not None else 0
        if c > eps and d <= eps:
            action = "charge"
            bkw = c
        elif d > eps and c <= eps:
            action = "discharge"
            bkw = d
        elif ic_int == 1 and c > eps:
            action = "charge"
            bkw = c
        elif ic_int == 0 and d > eps:
            action = "discharge"
            bkw = d
        else:
            action = "idle"
            bkw = 0.0

        plan.append(
            HourlyPlanEntry(
                hour=h.hour,
                grid_kwh=round(max(0.0, g), 4),
                solar_used_kwh=round(max(0.0, s), 4),
                battery_action=action,
                battery_kwh=round(max(0.0, bkw), 4),
                battery_energy_after_kwh=round(max(0.0, e_after), 4),
            )
        )

    plan.sort(key=lambda e: e.hour)
    return plan


def _best_effort_plan(
    *,
    sorted_hours: List[HourRecord],
    directives: MergedDirectives,
    max_charge: float,
    max_discharge: float,
    min_energy: float,
    initial_energy: float,
) -> List[HourlyPlanEntry]:
    """Best-effort fallback used when the LP solver reports infeasible/unbounded.

    The optimizer never lets ``grid_kwh`` go negative (it is hard-clamped at
    zero by construction in the LP), and we mirror that guarantee here.  When
    solar + battery cannot meet demand, the deficit is left as unmet demand
    rather than spilling into negative grid.  This keeps the public response
    shape stable (HTTP 200 with a 24-entry ``hourly_plan``) while remaining
    honest about what the schedule can deliver.
    """
    plan: List[HourlyPlanEntry] = []
    energy_after = float(initial_energy)
    for h in sorted_hours:
        hour_idx = h.hour
        directive_max_grid = directives.max_grid_kwh.get(hour_idx)
        cap = (
            float(directive_max_grid)
            if directive_max_grid is not None
            else float("inf")
        )
        demand = float(h.demand_kwh)
        solar_avail = effective_solar(
            float(h.solar_kwh),
            directives.effective_solar_factor(hour_idx),
        )
        # Use solar up to demand; never charge the battery in fallback mode
        # (it is a degraded path; we don't try to optimize).
        solar_used = min(solar_avail, demand)
        remaining = max(0.0, demand - solar_used)
        # Battery: discharge only, up to max_discharge and available energy.
        # In best-effort mode we may briefly drop below min_energy so the
        # demand can actually be met when the directive caps grid at 0.
        available = max(0.0, energy_after)
        max_disch = min(max_discharge, available) if max_discharge > 0 else 0.0
        discharge = min(remaining, max_disch)
        energy_after = max(0.0, energy_after - discharge)
        grid_needed = max(0.0, remaining - discharge)
        grid = min(grid_needed, cap)
        action = (
            "discharge" if discharge > 1e-6 else "idle"
        )
        plan.append(
            HourlyPlanEntry(
                hour=hour_idx,
                grid_kwh=round(max(0.0, grid), 4),
                solar_used_kwh=round(max(0.0, solar_used), 4),
                battery_action=action,
                battery_kwh=round(max(0.0, discharge), 4),
                battery_energy_after_kwh=round(max(0.0, energy_after), 4),
            )
        )
    plan.sort(key=lambda e: e.hour)
    return plan
