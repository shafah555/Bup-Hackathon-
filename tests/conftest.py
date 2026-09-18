"""Shared pytest fixtures: ensure deterministic, LLM-free test path."""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

# Force the offline/no-LLM path before importing app modules.
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("OFFLINE_FALLBACK", "true")
os.environ.setdefault("LLM_PROVIDER", "openai")
os.environ.setdefault("LLM_MODEL", "test-model")


class StubInterpreter:
    """Deterministic interpreter that the test suite monkey-patches in.

    It maps each note to a pre-canned interpretation list, provided via the
    ``stub_interpretations`` global.  This lets tests verify end-to-end
    pipeline behaviour without depending on any real LLM.
    """

    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self.interpretations: List[List[Any]] = []

    def interpret(self, notes: List[str]) -> List[Any]:
        self.calls.append(list(notes))
        if not self.interpretations:
            # Default: every note becomes no_op.
            return [
                _no_op(i)
                for i in range(len(notes))
            ]
        last = self.interpretations[-1]
        self.interpretations.pop()
        return last


def _no_op(idx: int) -> Any:
    from app.schemas import DirectiveInterpretation

    return DirectiveInterpretation(
        note_index=idx,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation="stub no_op",
    )


@pytest.fixture(autouse=True)
def _stub_interpreter(monkeypatch):
    """Replace the real LLM interpreter with a deterministic stub."""
    from app import llm_interpreter

    stub = StubInterpreter()
    monkeypatch.setattr(llm_interpreter, "_INTERPRETER", stub, raising=False)
    yield stub


@pytest.fixture
def stub(request):
    """Alias so tests can take the stub as a parameter named ``stub``."""
    # Reuse the autouse fixture above.
    return request.getfixturevalue("_stub_interpreter")
