"""Pydantic request/response schemas with strict validation."""
from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]


class HourRecord(BaseModel):
    """One hourly energy record."""

    model_config = ConfigDict(extra="forbid")

    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0, allow_inf_nan=False)
    solar_kwh: float = Field(..., ge=0, allow_inf_nan=False)
    tariff_bdt_per_kwh: float = Field(..., ge=0, allow_inf_nan=False)


class BatteryConfig(BaseModel):
    """Battery model configuration."""

    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float = Field(..., ge=0, allow_inf_nan=False)
    initial_energy_kwh: float = Field(..., ge=0, allow_inf_nan=False)
    minimum_energy_kwh: float = Field(..., ge=0, allow_inf_nan=False)
    max_charge_kwh_per_hour: float = Field(..., ge=0, allow_inf_nan=False)
    max_discharge_kwh_per_hour: float = Field(..., ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _check_invariants(self) -> "BatteryConfig":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise ValueError("initial_energy_kwh must be >= minimum_energy_kwh")
        return self


class OptimizeRequest(BaseModel):
    """Top-level POST /optimize-energy request body."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(..., min_length=1, max_length=128)
    operator_notes: List[str] = Field(..., min_length=1, max_length=3)
    hours: List[HourRecord] = Field(..., min_length=24, max_length=24)
    battery: BatteryConfig

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, value: List[str]) -> List[str]:
        if any((not isinstance(n, str)) or (not n.strip()) for n in value):
            raise ValueError("operator_notes entries must be non-empty strings")
        return value

    @field_validator("hours")
    @classmethod
    def _hours_complete(cls, value: List[HourRecord]) -> List[HourRecord]:
        seen = set()
        for h in value:
            if h.hour in seen:
                raise ValueError(f"duplicate hour {h.hour}")
            seen.add(h.hour)
        if seen != set(range(24)):
            raise ValueError("hours must cover exactly hours 0..23")
        return value


# ---- Directive interpretation ----


class SolarReductionAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    factor: float = Field(..., allow_inf_nan=False)


class MinimumBatteryReserveAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    minimum_energy_kwh: float = Field(..., allow_inf_nan=False)


class NoChargeWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]


class NoDischargeWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]


class MaxGridWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    max_grid_kwh: float = Field(..., allow_inf_nan=False)


StructuredAdjustment = Union[
    SolarReductionAdjustment,
    MinimumBatteryReserveAdjustment,
    NoChargeWindowAdjustment,
    NoDischargeWindowAdjustment,
    MaxGridWindowAdjustment,
    None,
]


class DirectiveInterpretation(BaseModel):
    """One structured directive produced from one operator note."""

    model_config = ConfigDict(extra="forbid")

    note_index: int = Field(..., ge=0, le=2)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[dict] = None
    explanation: str = ""


# ---- Hourly plan ----

BatteryAction = Literal["charge", "discharge", "idle"]


class HourlyPlanEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hour: int = Field(..., ge=0, le=23)
    grid_kwh: float = Field(..., ge=0, allow_inf_nan=False)
    solar_used_kwh: float = Field(..., ge=0, allow_inf_nan=False)
    battery_action: BatteryAction
    battery_kwh: float = Field(..., ge=0, allow_inf_nan=False)
    battery_energy_after_kwh: float = Field(..., ge=0, allow_inf_nan=False)


class OptimizeResponse(BaseModel):
    """Response body for POST /optimize-energy."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float = Field(..., allow_inf_nan=False)
    total_cost_bdt: float = Field(..., allow_inf_nan=False)
    peak_grid_kwh: float = Field(..., allow_inf_nan=False)
    plan_summary: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
