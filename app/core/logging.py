"""Logging configuration.

uvicorn's LOGGING_CONFIG declares no root logger, so application records escape through
`logging.lastResort`, which drops `extra` entirely — and every diagnostic here is an
`extra` dict. stdlib records go through structlog's ProcessorFormatter rather than
replacing `logging` calls, so third-party records land in the same stream and shape.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor

# Carry their own handlers: left alone they double-print and bypass our formatter.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

_HANDLER_NAME = "url-shortener-structlog"


def configure_logging(*, environment: str = "local", debug: bool = False) -> None:
    """Install one root handler rendering both structlog and stdlib records.

    JSON everywhere except `local`. Idempotent: re-running replaces our handler rather
    than stacking another, and leaves foreign handlers (pytest's caplog) alone.
    """
    level = logging.DEBUG if debug else logging.INFO
    render_json = environment != "local"

    # Applied to structlog and stdlib records alike, so both carry the same keys.
    shared_processors: list[Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.UnicodeDecoder(),
            # Hand off to the ProcessorFormatter below: one renderer per process.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if render_json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # ExtraAdder is load-bearing: without it every `extra={...}` field is discarded.
        foreign_pre_chain=[*shared_processors, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)
    handler.set_name(_HANDLER_NAME)

    root = logging.getLogger()
    for existing in root.handlers[:]:
        if getattr(existing, "name", None) == _HANDLER_NAME:
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    root.setLevel(level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        for existing in uvicorn_logger.handlers[:]:
            uvicorn_logger.removeHandler(existing)
        uvicorn_logger.propagate = True
