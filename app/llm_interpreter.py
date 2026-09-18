"""LLM-driven interpreter that turns operator notes into structured directives.

Architecture:
    Operator note(s)  ->  LLM (JSON mode)  ->  raw JSON
                                                    |
                                                    v
                                            guardrails.validate_full_response
                                                    |
                                                    v
                                            List[DirectiveInterpretation]

If the LLM is unavailable, misbehaves, or returns malformed JSON, the
interpreter raises ``InterpreterError``.  The caller (main.py) is responsible
for turning that into a controlled HTTP error.
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from .config import SETTINGS
from .errors import InterpreterError
from .guardrails import (
    parse_llm_response,
    safe_interpret_one_note,
    validate_full_response,
)
from .schemas import DirectiveInterpretation

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are GridWise Directive Interpreter, an expert system that
converts natural-language operator notes for a campus microgrid into STRICT
structured JSON directives.

ALLOWED DIRECTIVE TYPES (you MUST only use one of these per note):
  1. solar_reduction          - {"hours":[..],"factor":0..1}
  2. minimum_battery_reserve  - {"hours":[..],"minimum_energy_kwh":>=0}
  3. no_charge_window         - {"hours":[..]}
  4. no_discharge_window      - {"hours":[..]}
  5. max_grid_window          - {"hours":[..],"max_grid_kwh":>=0}
  6. no_op                    - null (for unrelated notes)

TIME INTERPRETATION RULES (very important):
- Hours are integers 0..23. Start hour is INCLUSIVE, end hour is EXCLUSIVE.
- "1 PM to 3 PM" -> [13, 14]
- "2 PM to 5 PM" -> [14, 15, 16]
- "noon until 2 PM" -> [12, 13]
- "between one and three in the afternoon" -> [13, 14]
- "from 13:00 until 15:00" -> [13, 14]
- "during the 6-9 PM window" -> [18, 19, 20]
- "13-15" -> [13, 14]
- Always deduplicate and sort ascending.

SOLAR REDUCTION RULES (critical - read carefully):
- factor = USABLE FRACTION REMAINING (0..1), NOT the percentage reduction.
- "solar drops to 20%" => factor 0.20
- "80% reduction" => factor 0.20
- "solar reduced by 50%" => factor 0.50
- "only one-quarter remains" => factor 0.25
- "30% of normal" => factor 0.30

OTHER RULES:
- Irrelevant notes (cafeteria menu, meeting rooms, sports registration,
  etc.) MUST be classified as no_op with applies=false and
  structured_adjustment=null.
- You MUST NOT modify demand, solar, tariff, or battery parameters.
- You MUST NOT compute the schedule.
- You MUST NOT invent unsupported directive types.
- Return ONE directive_interpretation object per operator note, in order,
  with note_index equal to the position of the note (0, 1, 2, ...).

OUTPUT FORMAT:
Return ONLY a JSON array, no prose, no markdown fences:

[
  {
    "note_index": 0,
    "applies": true,
    "directive_type": "solar_reduction",
    "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
    "explanation": "Solar reduced to 20% during the 1-3 PM maintenance window."
  },
  {
    "note_index": 1,
    "applies": false,
    "directive_type": "no_op",
    "structured_adjustment": null,
    "explanation": "Note about cafeteria menu does not affect the energy schedule."
  }
]
"""


def _build_user_prompt(notes: List[str]) -> str:
    return (
        "Interpret the following operator notes for a 24-hour campus "
        "energy schedule. Return a strict JSON array with one entry per "
        "note, in order. Do not add commentary.\n\n"
        "Operator notes:\n"
        + "\n".join(f"[{i}] {note}" for i, note in enumerate(notes))
    )


def _fallback_rule_based(notes: List[str]) -> List[DirectiveInterpretation]:
    """Safe offline fallback used only when the LLM is disabled/unavailable.

    The fallback is intentionally conservative: most notes default to no_op.
    It is NOT meant to be a complete interpreter - it exists only to keep
    the service responsive for local development.  In production with a
    working LLM, this branch is never taken.
    """
    out: List[DirectiveInterpretation] = []
    for i, note in enumerate(notes):
        if not SETTINGS.fallback_offline:
            raise InterpreterError(
                "LLM unavailable and offline fallback disabled",
                detail={"note_index": i},
            )
        logger.warning(
            "llm_fallback_used",
            extra={"note_index": i, "snippet": note[:60]},
        )
        out.append(safe_interpret_one_note(note))
        out[-1] = DirectiveInterpretation(
            note_index=i,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation="LLM unavailable; offline fallback classified note as no_op.",
        )
    return out


class LLMInterpreter:
    """Wraps a single LLM provider.  The instance is reusable across requests."""

    def __init__(self) -> None:
        self._client: Any = None
        self._provider = SETTINGS.llm_provider
        self._model = SETTINGS.llm_model
        self._api_key = SETTINGS.llm_api_key
        self._api_base = SETTINGS.llm_api_base
        self._timeout = SETTINGS.llm_timeout_seconds
        self._temperature = SETTINGS.llm_temperature
        self._initialized = False

    def _initialize(self) -> None:
        if self._initialized:
            return
        if not self._api_key:
            logger.warning("llm_no_api_key")
            self._initialized = True
            return
        try:
            if self._provider == "openai":
                import openai  # type: ignore

                kwargs = {"api_key": self._api_key, "timeout": self._timeout}
                if self._api_base:
                    kwargs["base_url"] = self._api_base
                self._client = openai.OpenAI(**kwargs)
            elif self._provider == "openai_compat":
                import openai  # type: ignore

                self._client = openai.OpenAI(
                    api_key=self._api_key,
                    base_url=self._api_base or "https://api.openai.com/v1",
                    timeout=self._timeout,
                )
            else:
                # Generic OpenAI-compatible chat completions endpoint.
                try:
                    import openai  # type: ignore

                    self._client = openai.OpenAI(
                        api_key=self._api_key,
                        base_url=self._api_base or "https://api.openai.com/v1",
                        timeout=self._timeout,
                    )
                except Exception:  # pragma: no cover
                    self._client = None
        except Exception as exc:  # pragma: no cover
            logger.warning("llm_init_failed", extra={"err": str(exc)})
            self._client = None
        self._initialized = True

    def interpret(self, notes: List[str]) -> List[DirectiveInterpretation]:
        """Interpret operator notes into validated directive interpretations."""
        if not notes:
            raise InterpreterError("No operator notes provided")
        self._initialize()

        if self._client is None:
            return _fallback_rule_based(notes)

        prompt = _build_user_prompt(notes)
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=self._temperature,
                response_format={"type": "json_object"} if self._supports_json_mode() else None,
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.warning("llm_request_failed", extra={"err": str(exc)[:120]})
            if SETTINGS.fallback_offline:
                return _fallback_rule_based(notes)
            raise InterpreterError(
                "LLM request failed",
                detail={"error": str(exc)[:200]},
            ) from exc

        text: Optional[str] = None
        try:
            text = response.choices[0].message.content
        except Exception as exc:
            raise InterpreterError(
                "LLM response missing content",
                detail={"err": str(exc)[:160]},
            ) from exc

        if not text:
            raise InterpreterError("LLM returned empty content")

        try:
            payload = parse_llm_response(text)
            # If model returned an object containing the array, unwrap it.
            if isinstance(payload, dict) and "interpretations" in payload and isinstance(payload["interpretations"], list):
                payload = payload["interpretations"]
            interpretations = validate_full_response(payload, notes)
        except Exception as exc:
            logger.warning("llm_validation_failed", extra={"err": str(exc)[:160]})
            raise InterpreterError(
                "LLM output failed guardrails",
                detail={"reason": str(exc)[:200]},
            ) from exc

        return interpretations

    def _supports_json_mode(self) -> bool:
        return self._provider in {"openai", "openai_compat"}


# Module-level singleton (re-used across requests for performance).
_INTERPRETER: Optional[LLMInterpreter] = None


def get_interpreter() -> LLMInterpreter:
    global _INTERPRETER
    if _INTERPRETER is None:
        _INTERPRETER = LLMInterpreter()
    return _INTERPRETER
