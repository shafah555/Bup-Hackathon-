"""Strict validation of LLM-produced directive interpretations."""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Tuple

from .calculations import normalize_hour_list
from .errors import GuardrailError
from .schemas import DirectiveInterpretation

ALLOWED_DIRECTIVES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


def _clean(value: Any) -> Any:
    """Best-effort cleanup of LLM-derived JSON-shaped payloads."""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return s
        # Strip code fences if the LLM wrapped JSON in ```json ... ```.
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:]
            s = s.strip()
            if s.endswith("```"):
                s = s[:-3].strip()
        return s
    return value


def parse_llm_response(raw: str) -> Any:
    """Parse LLM text to a Python object, tolerating common fences."""
    if not isinstance(raw, str):
        raise GuardrailError("LLM response was not text", detail={"type": type(raw).__name__})

    cleaned = _clean(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # Try to find the first {...} block.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise GuardrailError(
            "LLM returned non-JSON output",
            detail={"snippet": cleaned[:160]},
        ) from exc


def _extract_interpretations(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("interpretations", "directive_interpretation", "results", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                return [p for p in val if isinstance(p, dict)]
        # One interpretation passed directly.
        if "directive_type" in payload:
            return [payload]
        # Plain mapping of type->hours etc: wrap as single no-op to fail loudly.
        return []
    return []


def validate_interpretation_item(item: Dict[str, Any], note_index: int) -> DirectiveInterpretation:
    note_idx_raw = item.get("note_index", note_index)
    try:
        note_idx = int(note_idx_raw)
    except (TypeError, ValueError) as exc:
        raise GuardrailError("note_index missing or invalid", detail={"item": item}) from exc
    if note_idx != note_index:
        raise GuardrailError(
            "note_index does not match expected position",
            detail={"expected": note_index, "got": note_idx},
        )

    dtype = item.get("directive_type") or item.get("type")
    if dtype not in ALLOWED_DIRECTIVES:
        raise GuardrailError(
            "Unsupported directive_type from LLM",
            detail={"directive_type": dtype, "allowed": sorted(ALLOWED_DIRECTIVES)},
        )

    applies_raw = item.get("applies", True)
    if dtype == "no_op":
        if applies_raw is not False:
            # Accept truthy/falsy, but enforce final structure below.
            pass
        adj_in = item.get("structured_adjustment")
        if adj_in not in (None, {}, []):
            raise GuardrailError("no_op must not have structured_adjustment", detail={"adj": adj_in})
        return DirectiveInterpretation(
            note_index=note_index,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation=_safe_text(item.get("explanation") or "Note does not affect the schedule."),
        )

    # Non no_op: applies must be true.
    if not applies_raw:
        raise GuardrailError(
            "Non no_op directive must have applies=true",
            detail={"directive_type": dtype},
        )

    adj_in = item.get("structured_adjustment")
    if not isinstance(adj_in, dict):
        raise GuardrailError(
            "structured_adjustment must be an object for non no_op directives",
            detail={"directive_type": dtype, "adjustment": adj_in},
        )

    hours_field = adj_in.get("hours")
    if not isinstance(hours_field, list) or not all(isinstance(h, int) for h in hours_field):
        # Allow ints masquerading as floats.
        if not isinstance(hours_field, list):
            raise GuardrailError("structured_adjustment.hours must be a list", detail={"got": hours_field})
        try:
            hours_field = [int(h) for h in hours_field]
        except (TypeError, ValueError) as exc:
            raise GuardrailError("structured_adjustment.hours must be integers", detail={"got": hours_field}) from exc

    if any(h < 0 or h > 23 for h in hours_field):
        raise GuardrailError(
            "structured_adjustment.hours must contain only integers from 0 through 23",
            detail={"got": hours_field},
        )
    if hours_field != sorted(set(hours_field)):
        raise GuardrailError(
            "structured_adjustment.hours must be unique and ascending",
            detail={"got": hours_field},
        )
    norm_hours = hours_field
    if not norm_hours:
        raise GuardrailError(
            "structured_adjustment.hours must include at least one valid hour",
            detail={"got": hours_field},
        )

    if dtype == "solar_reduction":
        try:
            factor = float(adj_in["factor"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GuardrailError("solar_reduction requires factor", detail={"adj": adj_in}) from exc
        if not math.isfinite(factor) or not (0.0 <= factor <= 1.0):
            raise GuardrailError(
                "solar_reduction factor must be in [0,1]",
                detail={"factor": factor},
            )
        adj_out = {"hours": norm_hours, "factor": round(factor, 4)}

    elif dtype == "minimum_battery_reserve":
        try:
            min_e = float(adj_in["minimum_energy_kwh"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GuardrailError(
                "minimum_battery_reserve requires minimum_energy_kwh",
                detail={"adj": adj_in},
            ) from exc
        if not math.isfinite(min_e) or min_e < 0:
            raise GuardrailError(
                "minimum_energy_kwh must be finite and non-negative",
                detail={"got": min_e},
            )
        adj_out = {"hours": norm_hours, "minimum_energy_kwh": round(min_e, 4)}

    elif dtype == "no_charge_window":
        adj_out = {"hours": norm_hours}

    elif dtype == "no_discharge_window":
        adj_out = {"hours": norm_hours}

    elif dtype == "max_grid_window":
        try:
            cap = float(adj_in["max_grid_kwh"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GuardrailError("max_grid_window requires max_grid_kwh", detail={"adj": adj_in}) from exc
        if not math.isfinite(cap) or cap < 0:
            raise GuardrailError(
                "max_grid_window max_grid_kwh must be finite and non-negative",
                detail={"got": cap},
            )
        adj_out = {"hours": norm_hours, "max_grid_kwh": round(cap, 4)}

    else:  # pragma: no cover - guarded above
        raise GuardrailError("Unhandled directive_type", detail={"directive_type": dtype})

    return DirectiveInterpretation(
        note_index=note_index,
        applies=True,
        directive_type=dtype,
        structured_adjustment=adj_out,
        explanation=_safe_text(item.get("explanation")),
    )


def _safe_text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return default
    return str(value)


def validate_full_response(payload: Any, notes: List[str]) -> List[DirectiveInterpretation]:
    """Validate that the LLM JSON covers every operator note exactly once."""
    raw_list = _extract_interpretations(payload)
    if len(raw_list) != len(notes):
        raise GuardrailError(
            "LLM response must contain exactly one interpretation per operator note",
            detail={"expected": len(notes), "got": len(raw_list)},
        )

    seen_indices: List[int] = []
    validated: List[DirectiveInterpretation] = []
    for i, raw in enumerate(raw_list):
        item = validate_interpretation_item(raw, i)
        if item.note_index in seen_indices:
            raise GuardrailError(
                "Duplicate note_index from LLM",
                detail={"note_index": item.note_index},
            )
        seen_indices.append(item.note_index)
        validated.append(item)

    seen_indices.sort()
    expected = list(range(len(notes)))
    if seen_indices != expected:
        raise GuardrailError(
            "note_index values must be a contiguous 0..N-1 sequence",
            detail={"got": seen_indices, "expected": expected},
        )
    return validated


def ensure_all_notes_covered(
    interpreted: List[DirectiveInterpretation],
    notes: List[str],
) -> List[DirectiveInterpretation]:
    """Defensive guarantee: if the LLM ever returns fewer items, fill the rest with no_op."""
    out: List[DirectiveInterpretation] = list(interpreted)
    have = {i.note_index for i in out}
    for i in range(len(notes)):
        if i not in have:
            out.append(
                DirectiveInterpretation(
                    note_index=i,
                    applies=False,
                    directive_type="no_op",
                    structured_adjustment=None,
                    explanation="Operator note did not affect the schedule.",
                )
            )
    out.sort(key=lambda d: d.note_index)
    return out


def safe_interpret_one_note(note: str) -> DirectiveInterpretation:
    """Build a no_op interpretation for a single note (used as a strict fallback)."""
    return DirectiveInterpretation(
        note_index=0,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation="LLM unavailable; defaulting to no_op.",
    )
