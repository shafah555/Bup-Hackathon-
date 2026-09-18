"""LLM-driven interpreter that turns operator notes into structured directives.

Architecture:
    Operator note(s)  ->  LLM (OpenRouter / OpenAI-compatible)  ->  raw JSON
                                                                       |
                                                                       v
                                                     guardrails.validate_full_response
                                                                       |
                                                                       v
                                                     List[DirectiveInterpretation]

Failure ladder (each step only runs if the previous one failed):
    1. primary model            (LLM_MODEL)
    2. backup models            (LLM_FALLBACK_MODELS, comma separated, optional)
    3. deterministic rule-based parser   (only if OFFLINE_FALLBACK=true)
    4. InterpreterError -> controlled HTTP error (if OFFLINE_FALLBACK=false)

OpenRouter support:
    * LLM_PROVIDER=openrouter  (or an API key starting with "sk-or-") makes the
      base URL default to https://openrouter.ai/api/v1
    * OpenRouter attribution headers are sent automatically.
    * If a model rejects ``response_format=json_object`` the call is retried
      once without it (many free models do not support JSON mode).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Optional, Tuple

from .config import SETTINGS
from .errors import InterpreterError
from .guardrails import parse_llm_response, validate_full_response
from .schemas import DirectiveInterpretation

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"


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
- "0 to 6" -> [0, 1, 2, 3, 4, 5]
- "entire day", "all day", "whole day", "throughout the day" -> [0,1,2,...,23]
- Always deduplicate and sort ascending.

SOLAR REDUCTION RULES (critical - read carefully):
- factor = USABLE FRACTION REMAINING (0..1), NOT the percentage reduction.
- "solar drops to 20%" => factor 0.20
- "80% reduction" => factor 0.20
- "solar reduced by 50%" => factor 0.50
- "only one-quarter remains" => factor 0.25
- "one-fifth of normal output" => factor 0.20
- "30% of normal" => factor 0.30

OTHER RULES:
- "Do not draw from / discharge the battery" => no_discharge_window.
- "Charging unavailable / do not charge" => no_charge_window.
- "Limit grid import to X kWh" => max_grid_window with max_grid_kwh = X.
- "Keep battery at least X kWh" => minimum_battery_reserve with minimum_energy_kwh = X.
- Irrelevant notes (cafeteria menu, meeting rooms, sports registration,
  etc.) MUST be classified as no_op with applies=false and
  structured_adjustment=null.
- You MUST NOT modify demand, solar, tariff, or battery parameters.
- You MUST NOT compute the schedule.
- You MUST NOT invent unsupported directive types.
- Return ONE directive_interpretation object per operator note, in order,
  with note_index equal to the position of the note (0, 1, 2, ...).

OUTPUT FORMAT:
Return ONLY JSON, no prose, no markdown fences. Wrap the array in an object
under the key "interpretations":

{"interpretations": [
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
]}
"""


def _build_user_prompt(notes: List[str]) -> str:
    return (
        "Interpret the following operator notes for a 24-hour campus "
        "energy schedule. Return strict JSON with one entry per "
        "note, in order. Do not add commentary.\n\n"
        "Operator notes:\n"
        + "\n".join(f"[{i}] {note}" for i, note in enumerate(notes))
    )


# ---------------------------------------------------------------------------
# Deterministic rule-based fallback parser (used only when the LLM fails and
# OFFLINE_FALLBACK=true).  Much smarter than a blanket no_op, so the API still
# returns correct schedules for common notes even if OpenRouter is down or
# rate-limited.
# ---------------------------------------------------------------------------
_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_FRACTION_WORDS = [
    (r"one[-\s]?fifth", 0.2), (r"one[-\s]?quarter|a quarter|quarter", 0.25),
    (r"one[-\s]?third|a third", 1 / 3), (r"one[-\s]?half|a half|half", 0.5),
    (r"two[-\s]?thirds", 2 / 3), (r"three[-\s]?quarters", 0.75),
    (r"one[-\s]?tenth", 0.1),
]
_ALL_DAY = re.compile(
    r"\b(entire day|whole day|all day|full day|throughout the day|all 24 hours|24 hours|"
    r"all hours|the day)\b", re.I,
)
_NEGATION = re.compile(
    r"\b(do not|don't|dont|no|not|never|unavailable|disable[d]?|prohibit(?:ed)?|avoid|"
    r"forbid(?:den)?|must not|cannot|can't|halt|stop|suspend(?:ed)?|freeze|block(?:ed)?|"
    r"offline|out of service)\b", re.I,
)


def _to_24h(hour: int, meridiem: Optional[str]) -> int:
    if meridiem == "pm":
        return hour % 12 + 12
    if meridiem == "am":
        return hour % 12
    return hour


def _expand(start: int, end: int) -> List[int]:
    """Start inclusive, end exclusive; supports wrap past midnight."""
    end = 24 if end == 0 and start > 0 else end
    if end > start:
        return [h for h in range(start, end) if 0 <= h <= 23]
    if end < start:  # wraps past midnight
        return sorted(set(list(range(start, 24)) + list(range(0, end))))
    return []


def _parse_hours(text: str) -> Optional[List[int]]:
    low = text.lower()
    if _ALL_DAY.search(low) and not re.search(r"\d{1,2}\s*(?::\d{2})?\s*(am|pm)?\s*(?:to|until|till|-|–|and)\s*\d", low):
        return list(range(24))

    # Remove kWh / percentage amounts so they are not mistaken for times.
    cleaned = re.sub(r"\d+(?:\.\d+)?\s*(?:kwh|kw|%|percent)", " ", low)
    # Word numbers -> digits ("from one until three" -> "from 1 until 3").
    used_words = False
    words = "|".join(_WORD_NUM)
    word_range = re.compile(
        rf"\b({words}|\d{{1,2}})\b(\s*(?:to|until|till|and|-|–)\s*)\b({words}|\d{{1,2}})\b"
    )
    wm = word_range.search(cleaned)
    if wm and (wm.group(1) in _WORD_NUM or wm.group(3) in _WORD_NUM):
        a = str(_WORD_NUM.get(wm.group(1), wm.group(1)))
        b = str(_WORD_NUM.get(wm.group(3), wm.group(3)))
        cleaned = cleaned[: wm.start()] + f"{a}{wm.group(2)}{b}" + cleaned[wm.end():]
        used_words = True
    cleaned = re.sub(r"\bnoon\b", "12:00", cleaned)
    cleaned = re.sub(r"\bmidnight\b", "0:00", cleaned)

    pattern = re.compile(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*"
        r"(?:to|until|till|through|thru|and|-|–|—)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
    )
    m = pattern.search(cleaned)
    if not m:
        return None

    sh, eh = int(m.group(1)), int(m.group(4))
    sm = (m.group(3) or "").replace(".", "")[:2] or None
    em = (m.group(6) or "").replace(".", "")[:2] or None
    if sh > 24 or eh > 24:
        return None

    if sm is None and em is not None:
        sm = em
        # "10 to 2 PM" -> 10 AM to 2 PM
        if _to_24h(sh, sm) >= _to_24h(eh, em) and sh != 12 and sm == "pm":
            sm = "am"
    if em is None and sm is not None:
        em = sm
    if sm is None and em is None and used_words and 1 <= sh <= 6 and 1 <= eh <= 12 and "morning" not in cleaned:
        sm = em = "pm"  # "one until three" (spoken, no am/pm) -> afternoon

    start = _to_24h(sh, sm)
    end = _to_24h(eh, em)
    hours = _expand(start, end)
    return hours or None


def _extract_kwh(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", text, re.I)
    return float(m.group(1)) if m else None


def _extract_solar_factor(text: str) -> Optional[float]:
    low = text.lower()
    if re.search(r"\b(no|zero)\s+(?:solar|pv|sun)", low) or "completely" in low and "solar" in low and "unavailable" in low:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", low)
    if m:
        pct = float(m.group(1)) / 100.0
        # "80% reduction", "reduced by 50%", "cut by 30%"  -> usable = 1 - pct
        around = low[max(0, m.start() - 25): m.end() + 25]
        if re.search(r"(reduc\w*|cut|drop\w*|decreas\w*|lower\w*|loss|less)\s+(?:by|of)?\s*(?:about|roughly|around)?\s*"
                     + re.escape(m.group(1)) + r"\s*(?:%|percent)", around) or re.search(
                re.escape(m.group(1)) + r"\s*(?:%|percent)\s*(?:reduction|drop|decrease|loss|cut|less)", around):
            return round(max(0.0, min(1.0, 1.0 - pct)), 4)
        return round(max(0.0, min(1.0, pct)), 4)
    for pat, val in _FRACTION_WORDS:
        if re.search(pat, low):
            return round(val, 4)
    return None


def _rule_based_one(note: str, idx: int) -> DirectiveInterpretation:
    low = note.lower()
    hours = _parse_hours(note)
    kwh = _extract_kwh(note)

    def noop(reason: str = "Note does not affect the energy schedule.") -> DirectiveInterpretation:
        return DirectiveInterpretation(
            note_index=idx, applies=False, directive_type="no_op",
            structured_adjustment=None, explanation=reason,
        )

    def make(dtype: str, adj: dict, explanation: str) -> DirectiveInterpretation:
        return DirectiveInterpretation(
            note_index=idx, applies=True, directive_type=dtype,
            structured_adjustment=adj, explanation=explanation,
        )

    has_solar = bool(re.search(r"\b(solar|pv|photovoltaic|rooftop|panels?)\b", low))
    has_battery = bool(re.search(r"\b(battery|batteries|storage|bess)\b", low))
    has_grid = bool(re.search(r"\b(grid|import|imports|utility|mains)\b", low))

    # 1) solar reduction
    if has_solar:
        factor = _extract_solar_factor(note)
        if factor is not None and hours:
            return make("solar_reduction", {"hours": hours, "factor": factor},
                        f"Solar limited to {factor:.0%} of normal output during hours {hours[0]}-{hours[-1] + 1}.")

    # 2) minimum battery reserve
    if kwh is not None and (has_battery or "reserve" in low) and re.search(
        r"(reserve|at least|minimum|min\b|fall below|drop below|go below|stay above|keep|maintain|hold|not below)", low
    ) and not (has_grid and re.search(r"(limit|cap|no more than|at most|maximum|max\b|exceed)", low) and not has_battery):
        if hours:
            return make("minimum_battery_reserve", {"hours": hours, "minimum_energy_kwh": kwh},
                        f"Battery energy kept at or above {kwh:g} kWh.")

    # 3) grid cap
    if kwh is not None and has_grid and re.search(
        r"(limit|cap|no more than|at most|maximum|max\b|not exceed|under|up to|ceiling|restrict)", low
    ):
        if hours:
            return make("max_grid_window", {"hours": hours, "max_grid_kwh": kwh},
                        f"Grid import capped at {kwh:g} kWh per hour.")

    # 4) no discharge
    if re.search(r"(discharg|draw(?:ing)? (?:from|on) (?:the )?(?:battery|storage)|use (?:of )?(?:the )?battery|"
                 r"drain|battery (?:power|use|usage|output))", low) and _NEGATION.search(low):
        h = hours or (list(range(24)) if _ALL_DAY.search(low) else None)
        if h:
            return make("no_discharge_window", {"hours": h}, "Battery discharge is blocked in this window.")

    # 5) no charge
    if re.search(r"(?<!dis)charg", low) and _NEGATION.search(low):
        h = hours or (list(range(24)) if _ALL_DAY.search(low) else None)
        if h:
            return make("no_charge_window", {"hours": h}, "Battery charging is blocked in this window.")

    return noop()


def _fallback_rule_based(notes: List[str], reason: str = "llm_unavailable") -> List[DirectiveInterpretation]:
    """Deterministic parser; used only when the LLM path failed."""
    if not SETTINGS.fallback_offline:
        raise InterpreterError(
            "LLM unavailable and offline fallback disabled",
            detail={"reason": reason},
        )
    out: List[DirectiveInterpretation] = []
    for i, note in enumerate(notes):
        logger.warning("llm_fallback_used note_index=%s reason=%s snippet=%r", i, reason, note[:60])
        try:
            out.append(_rule_based_one(note, i))
        except Exception:  # never let the fallback crash the request
            out.append(DirectiveInterpretation(
                note_index=i, applies=False, directive_type="no_op",
                structured_adjustment=None,
                explanation="Offline fallback could not interpret this note; treated as no_op.",
            ))
    return out


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------
def _is_openrouter(provider: str, api_key: Optional[str], api_base: Optional[str]) -> bool:
    return (
        provider == "openrouter"
        or (api_base is not None and "openrouter.ai" in api_base)
        or (api_key is not None and api_key.startswith("sk-or-"))
    )


class LLMInterpreter:
    """Wraps a single OpenAI-compatible provider (OpenRouter, OpenAI, Groq...)."""

    def __init__(self) -> None:
        self._client: Any = None
        self._provider = SETTINGS.llm_provider
        self._api_key = SETTINGS.llm_api_key
        self._api_base = SETTINGS.llm_api_base
        self._timeout = SETTINGS.llm_timeout_seconds
        self._temperature = SETTINGS.llm_temperature
        self._initialized = False
        self._openrouter = _is_openrouter(self._provider, self._api_key, self._api_base)

        # Model list: primary + optional comma-separated backups.
        models = [SETTINGS.llm_model]
        extra = os.environ.get("LLM_FALLBACK_MODELS", "")
        models += [m.strip() for m in extra.split(",") if m.strip()]
        seen = set()
        self._models = [m for m in models if not (m in seen or seen.add(m))]

    def _initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        if not self._api_key:
            logger.warning("llm_no_api_key - using offline fallback parser")
            return
        try:
            import openai  # type: ignore

            base = self._api_base or (OPENROUTER_BASE_URL if self._openrouter else OPENAI_BASE_URL)
            kwargs: dict = {"api_key": self._api_key, "base_url": base, "timeout": self._timeout, "max_retries": 1}
            if self._openrouter:
                kwargs["default_headers"] = {
                    "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://bup-hackathon-18a5.onrender.com"),
                    "X-Title": os.environ.get("OPENROUTER_TITLE", "GridWise"),
                }
            self._client = openai.OpenAI(**kwargs)
            logger.info("llm_client_ready base=%s models=%s", base, self._models)
        except Exception as exc:  # pragma: no cover
            logger.warning("llm_init_failed err=%s", str(exc)[:160])
            self._client = None

    def _call_model(self, model: str, prompt: str) -> str:
        """Call one model; retry once without JSON mode if it is unsupported."""
        base_kwargs: dict = dict(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=self._temperature,
            max_tokens=1500,
            timeout=self._timeout,
        )
        try:
            resp = self._client.chat.completions.create(
                response_format={"type": "json_object"}, **base_kwargs
            )
        except Exception as exc:
            msg = str(exc).lower()
            json_mode_problem = any(
                s in msg for s in ("response_format", "json_object", "json mode", "structured", "400")
            )
            if not json_mode_problem:
                raise
            logger.warning("llm_json_mode_rejected model=%s - retrying without response_format", model)
            resp = self._client.chat.completions.create(**base_kwargs)

        text = resp.choices[0].message.content
        if not text or not text.strip():
            raise InterpreterError("LLM returned empty content", detail={"model": model})
        return text

    def _try_models(self, notes: List[str]) -> Tuple[Optional[List[DirectiveInterpretation]], str]:
        prompt = _build_user_prompt(notes)
        last_err = "no_models"
        for model in self._models:
            try:
                text = self._call_model(model, prompt)
                payload = parse_llm_response(text)
                result = validate_full_response(payload, notes)
                logger.info("llm_ok model=%s", model)
                return result, ""
            except Exception as exc:
                last_err = f"{model}: {type(exc).__name__}: {str(exc)[:160]}"
                logger.warning("llm_model_failed %s", last_err)
        return None, last_err

    def interpret(self, notes: List[str]) -> List[DirectiveInterpretation]:
        """Interpret operator notes into validated directive interpretations."""
        if not notes:
            raise InterpreterError("No operator notes provided")
        self._initialize()

        if self._client is None:
            return _fallback_rule_based(notes, reason="no_api_key_or_client")

        result, err = self._try_models(notes)
        if result is not None:
            return result

        if SETTINGS.fallback_offline:
            return _fallback_rule_based(notes, reason=err)
        raise InterpreterError("LLM interpretation failed", detail={"reason": err[:200]})


# Module-level singleton (re-used across requests for performance).
_INTERPRETER: Optional[LLMInterpreter] = None


def get_interpreter() -> LLMInterpreter:
    global _INTERPRETER
    if _INTERPRETER is None:
        _INTERPRETER = LLMInterpreter()
    return _INTERPRETER