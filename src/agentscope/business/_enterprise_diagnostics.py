# -*- coding: utf-8 -*-
"""Record failure metadata without including upstream payloads or host paths."""

import httpx

from agentscope._logging import logger


def report_failure(
    code: str,
    stage: str,
    error: Exception,
    *,
    step: str | None = None,
) -> dict:
    """Return safe diagnostic fields shared by import receipts and checkpoints."""
    context: dict[str, str | int] = {
        "stage": stage,
        "error_type": type(error).__name__,
    }
    if isinstance(error, httpx.HTTPStatusError):
        context["http_status"] = error.response.status_code
    if step:
        context["step"] = step[:80]
    logger.warning("Enterprise business %s: %s", code, context)
    return {"code": code, "context": context}
