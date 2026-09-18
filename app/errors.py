"""Controlled error classes and FastAPI error handler mapping."""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class GridWiseError(Exception):
    """Base controlled error type for the GridWise service."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "gridwise_error"

    def __init__(self, message: str, *, detail: Optional[Any] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ValidationError(GridWiseError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"


class GuardrailError(GridWiseError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "guardrail_error"


class InterpreterError(GridWiseError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "interpreter_error"


class OptimizerError(GridWiseError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "optimizer_error"


class ValidatorError(GridWiseError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "validator_error"


def _safe_format_detail(detail: Any) -> Any:
    """Filter sensitive data before returning in error responses."""
    if detail is None:
        return None
    if isinstance(detail, dict):
        clean = {}
        for k, v in detail.items():
            lk = k.lower()
            if any(secret in lk for secret in ("api_key", "apikey", "token", "secret", "password")):
                clean[k] = "***redacted***"
            else:
                clean[k] = _safe_format_detail(v)
        return clean
    if isinstance(detail, list):
        return [_safe_format_detail(x) for x in detail]
    return detail


async def gridwise_error_handler(request: Request, exc: GridWiseError) -> JSONResponse:
    # NOTE: do not pass exc.message via ``extra`` because Python's logging
    # module reserves the attribute name ``message`` and would raise
    # ``KeyError: Attempt to overwrite 'message' in LogRecord`` on >=3.12.
    logger.warning(
        "controlled_error code=%s msg=%s path=%s",
        exc.code,
        exc.message,
        request.url.path,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.code,
            "detail": exc.message,
            "context": _safe_format_detail(exc.detail),
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error path=%s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "detail": "An unexpected error occurred while processing the request.",
            "context": None,
        },
    )
