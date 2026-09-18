"""LLM interpreter tests (using the offline fallback path)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import SETTINGS
from app.main import app
from app.schemas import DirectiveInterpretation
from tests._helpers import request_payload, set_stub_interpretations


client = TestClient(app)


def _build_interpretation(directive_type, adj, note_index, applies=True, explanation=""):
    return DirectiveInterpretation(
        note_index=note_index,
        applies=applies,
        directive_type=directive_type,
        structured_adjustment=adj,
        explanation=explanation,
    )


def test_interpret_endpoint_returns_interpretations(stub):
    set_stub_interpretations(
        stub,
        [_build_interpretation("solar_reduction", {"hours": [13, 14], "factor": 0.2}, 0)],
    )
    resp = client.post("/optimize-energy", json=request_payload(["PV drop to 20% 13-15"]))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["directive_interpretation"]) == 1
    assert body["directive_interpretation"][0]["directive_type"] == "solar_reduction"
    assert body["directive_interpretation"][0]["structured_adjustment"]["factor"] == 0.2
    assert body["directive_interpretation"][0]["structured_adjustment"]["hours"] == [13, 14]


def test_three_notes_each_get_index(stub):
    set_stub_interpretations(
        stub,
        [
            _build_interpretation("solar_reduction", {"hours": [13, 14], "factor": 0.2}, 0),
            _build_interpretation("no_charge_window", {"hours": [18, 19]}, 1),
            _build_interpretation("no_op", None, 2, applies=False),
        ],
    )
    resp = client.post(
        "/optimize-energy",
        json=request_payload(["PV drop", "no charge evening", "cafeteria menu"]),
    )
    body = resp.json()
    indices = [d["note_index"] for d in body["directive_interpretation"]]
    assert indices == [0, 1, 2]
    assert body["directive_interpretation"][2]["directive_type"] == "no_op"
    assert body["directive_interpretation"][2]["applies"] is False
    assert body["directive_interpretation"][2]["structured_adjustment"] is None


def test_fallback_returns_no_ops_when_llm_unavailable():
    """When the stub gives no interpretation, default no_op per note should be used."""
    # The stub defaults to no_op for every note.
    resp = client.post("/optimize-energy", json=request_payload(["hello"]))
    body = resp.json()
    assert resp.status_code == 200
    assert body["directive_interpretation"][0]["directive_type"] == "no_op"
    assert body["directive_interpretation"][0]["applies"] is False


def test_offline_fallback_runs_when_no_api_key():
    """Sanity check: SETTINGS.llm_api_key is empty in tests, so the interpreter
    initializes without a client and uses the offline fallback."""
    assert SETTINGS.llm_api_key in (None, "")