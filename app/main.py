"""GridWise FastAPI service.

Pipeline per request:

    request -> Pydantic validation -> LLM interpreter -> guardrails
            -> directive merge -> LP optimizer -> validator
            -> recomputed totals -> OptimizeResponse
"""
from __future__ import annotations

import logging
import time
from typing import List

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .calculations import plan_totals
from .config import SETTINGS
from .directives import MergedDirectives, describe_merged_directives, merge_directives
from .errors import (
    GuardrailError,
    GridWiseError,
    InterpreterError,
    OptimizerError,
    ValidatorError,
    gridwise_error_handler,
    unhandled_error_handler,
)
from .llm_interpreter import get_interpreter
from .optimizer import optimize_schedule
from .schemas import (
    HealthResponse,
    HourlyPlanEntry,
    OptimizeRequest,
    OptimizeResponse,
)
from .validator import validate_final_response

logger = logging.getLogger("gridwise")
logging.basicConfig(level=SETTINGS.log_level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")


app = FastAPI(
    title="GridWise - Smart Campus Energy Optimization",
    version="1.0.0",
    description="LLM-assisted 24-hour energy schedule optimizer.",
)


app.add_exception_handler(GridWiseError, gridwise_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.info("request_validation_error", extra={"path": request.url.path})
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "detail": "Request payload failed schema validation.",
            "context": exc.errors(),
        },
    )


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    """Serve the responsive operator workspace at the deployment root."""

    return FileResponse("app/static/index.html", media_type="text/html")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


def _build_plan_summary(
    merged: MergedDirectives,
    initial_energy: float,
    total_cost: float,
    total_grid: float,
) -> str:
    summary = (
        "GridWise schedule applies "
        + describe_merged_directives(merged)
        + f". The optimizer uses solar where available, shifts battery energy "
        f"toward higher-tariff hours while respecting reserve and rate limits, "
        f"restores the initial battery level ({initial_energy:.1f} kWh) at the "
        f"end of the day, and yields a total grid consumption of "
        f"{total_grid:.2f} kWh at a total cost of {total_cost:.2f} BDT."
    )
    return summary


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(payload: OptimizeRequest) -> OptimizeResponse:
    start = time.perf_counter()
    scenario_id = payload.scenario_id
    notes = payload.operator_notes
    hours = payload.hours
    battery = payload.battery

    logger.info("optimize_request", extra={"scenario_id": scenario_id, "notes": len(notes)})

    interpreter = get_interpreter()
    try:
        interpretations = interpreter.interpret(notes)
    except InterpreterError as exc:
        logger.warning("interpretation_failed", extra={"scenario_id": scenario_id})
        # Surface the failure as a controlled error; do not silently invent.
        raise

    merged: MergedDirectives = merge_directives(interpretations)
    for hour, reserve in merged.battery_reserve.items():
        if reserve > battery.capacity_kwh:
            raise GuardrailError(
                "minimum_battery_reserve cannot exceed battery capacity",
                detail={"hour": hour, "reserve": reserve, "capacity": battery.capacity_kwh},
            )
    logger.info(
        "directives_merged",
        extra={
            "scenario_id": scenario_id,
            "active_types": sorted(merged.active_directive_types),
        },
    )

    try:
        plan: List[HourlyPlanEntry] = optimize_schedule(hours, battery, merged)
    except OptimizerError:
        raise

    # Recompute totals from the final plan.
    total_grid, total_cost, peak_grid = plan_totals(plan, hours)

    # Independent validation before returning.
    try:
        validate_final_response(
            plan=plan,
            hours=hours,
            battery=battery,
            directives=merged,
            interpretations=interpretations,
            notes=notes,
            reported_totals={
                "total_grid_kwh": total_grid,
                "total_cost_bdt": total_cost,
                "peak_grid_kwh": peak_grid,
            },
        )
    except ValidatorError:
        raise

    summary = _build_plan_summary(merged, battery.initial_energy_kwh, total_cost, total_grid)

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    logger.info(
        "optimize_done",
        extra={
            "scenario_id": scenario_id,
            "elapsed_ms": round(elapsed_ms, 1),
            "total_cost_bdt": round(total_cost, 2),
            "peak_grid_kwh": round(peak_grid, 2),
        },
    )

    return OptimizeResponse(
        scenario_id=scenario_id,
        directive_interpretation=interpretations,
        hourly_plan=plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=summary,
    )