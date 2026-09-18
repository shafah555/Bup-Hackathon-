"""Guardrails tests."""
from __future__ import annotations

import json

import pytest

from app.errors import GuardrailError
from app.guardrails import (
    parse_llm_response,
    validate_full_response,
    validate_interpretation_item,
    ensure_all_notes_covered,
)
from app.schemas import DirectiveInterpretation


def test_parses_json_with_fences():
    payload = parse_llm_response("```json\n[{\"a\": 1}]\n```")
    assert payload == [{"a": 1}]


def test_rejects_non_json():
    with pytest.raises(GuardrailError):
        parse_llm_response("hello world")


def test_accepts_object_wrapping_array():
    text = json.dumps({"interpretations": [{"note_index": 0, "directive_type": "no_op", "applies": False, "structured_adjustment": None}]})
    payload = parse_llm_response(text)
    assert "interpretations" in payload


def test_invalid_directive_type_rejected():
    with pytest.raises(GuardrailError):
        validate_interpretation_item({"note_index": 0, "applies": True, "directive_type": "fake_directive", "structured_adjustment": {}}, 0)


def test_solar_factor_out_of_range():
    with pytest.raises(GuardrailError):
        validate_interpretation_item(
            {"note_index": 0, "applies": True, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [10], "factor": 1.5}},
            0,
        )


def test_no_op_forces_applies_false():
    out = validate_interpretation_item(
        {"note_index": 0, "applies": True, "directive_type": "no_op", "structured_adjustment": None},
        0,
    )
    assert out.applies is False
    assert out.directive_type == "no_op"
    assert out.structured_adjustment is None


def test_non_noop_applies_false_rejected():
    with pytest.raises(GuardrailError):
        validate_interpretation_item(
            {"note_index": 0, "applies": False, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [10], "factor": 0.5}},
            0,
        )


def test_hours_must_be_unique_and_ascending():
    with pytest.raises(GuardrailError):
        validate_interpretation_item(
            {"note_index": 0, "applies": True, "directive_type": "no_discharge_window", "structured_adjustment": {"hours": [22, 18, 18, 19]}},
            0,
        )


def test_hours_out_of_range_rejected():
    with pytest.raises(GuardrailError):
        validate_interpretation_item(
            {"note_index": 0, "applies": True, "directive_type": "no_discharge_window", "structured_adjustment": {"hours": [25, 18, 19]}},
            0,
        )


def test_hours_duplicates_or_unsorted_rejected():
    with pytest.raises(GuardrailError):
        validate_interpretation_item(
            {"note_index": 0, "applies": True, "directive_type": "no_discharge_window", "structured_adjustment": {"hours": [19, 18, 18]}},
            0,
        )


def test_non_finite_numbers_rejected():
    with pytest.raises(GuardrailError):
        validate_interpretation_item(
            {"note_index": 0, "applies": True, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [10], "factor": "inf"}},
            0,
        )


def test_validate_full_response_requires_one_per_note():
    payload = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [10], "factor": 0.5}}]
    with pytest.raises(GuardrailError):
        validate_full_response(payload, ["a", "b"])


def test_validate_full_response_rejects_out_of_order_indices():
    payload = [
        {"note_index": 1, "applies": False, "directive_type": "no_op", "structured_adjustment": None},
        {"note_index": 0, "applies": True, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [10], "factor": 0.5}},
    ]
    with pytest.raises(GuardrailError):
        validate_full_response(payload, ["a", "b"])


def test_ensure_all_notes_covered_fills_missing():
    existing = [
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="solar_reduction",
            structured_adjustment={"hours": [10], "factor": 0.5}, explanation="x"
        )
    ]
    out = ensure_all_notes_covered(existing, ["n1", "n2", "n3"])
    assert [d.note_index for d in out] == [0, 1, 2]
    assert out[1].directive_type == "no_op"
    assert out[2].directive_type == "no_op"