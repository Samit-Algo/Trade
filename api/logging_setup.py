"""Request logging for order endpoints.

An HTTP port that can place orders needs to record who asked. The audit trail
in tiger_backend/audit.py already records WHAT the order was; this records
WHERE the request came from, alongside it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

LOG_DIRECTORY = Path(__file__).resolve().parent.parent / "logs"
API_LOG_PATH = LOG_DIRECTORY / "api_requests.log"

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Return the API request logger, configuring it once.

    Returns:
        A logger writing to logs/api_requests.log.
    """
    global _logger
    if _logger is not None:
        return _logger

    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("tiger_api")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.FileHandler(API_LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger


def log_order_request(request, action: str, subject: str, cash: float | None) -> None:
    """Record an order-path request with the address that made it.

    Args:
        request: The FastAPI request, for the client address.
        action: preview, submit or cancel.
        subject: The contract identifier or order ID.
        cash: The cash figure involved, when there is one.
    """
    client_host = request.client.host if request.client else "unknown"
    cash_text = f"{cash:.2f}" if cash is not None else "-"

    get_logger().info(
        "action=%s client=%s subject=%s cash=%s at=%s",
        action,
        client_host,
        subject,
        cash_text,
        datetime.now(timezone.utc).isoformat(),
    )
