"""Structured JSON logging.

Every line carries ``run_id`` and ``trace_id`` once bound, so a run can be
reconstructed from logs alone. Logs are a debugging aid only: evaluation numbers
come from the SQLite ledger, never from log parsing.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_configured = False


class _CurrentStderr:
    """Writes to whatever ``sys.stderr`` is at the moment of the write.

    structlog's print factory binds a file object once, which breaks as soon as
    anything replaces ``sys.stderr`` afterwards: pytest's capture, a context
    manager, a supervisor reopening the stream. Resolving it per write keeps
    logging working for the life of the process instead of until the first
    reassignment.
    """

    def write(self, message: str) -> int:
        """Write to the current stderr."""
        return sys.stderr.write(message)

    def flush(self) -> None:
        """Flush the current stderr."""
        sys.stderr.flush()


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure structlog and the standard library root logger.

    Safe to call more than once; later calls reconfigure rather than stack
    processors.

    Args:
        level: Standard logging level name, for example ``INFO``.
        json_output: Emit JSON lines when true, human-readable console output
            when false.
    """
    global _configured

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=_CurrentStderr()),  # type: ignore[arg-type]
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level.upper(), stream=sys.stderr, format="%(message)s", force=True)
    _configured = True


def get_logger(
    name: str | None = None,
    **initial: Any,  # noqa: ANN401 - structlog binds arbitrary values by design
) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring logging on first use.

    Args:
        name: Logger name, conventionally the module ``__name__``.
        **initial: Key-value pairs bound to every line from this logger.

    Returns:
        A bound structlog logger.
    """
    if not _configured:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger.bind(**initial) if initial else logger


def bind_run(run_id: str, trace_id: str | None = None) -> None:
    """Bind run identifiers to every subsequent log line in this context.

    Args:
        run_id: The ProofPR run identifier, for example ``r-7f3a``.
        trace_id: The OpenTelemetry trace ID when tracing is enabled.
    """
    structlog.contextvars.bind_contextvars(run_id=run_id)
    if trace_id is not None:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)


def clear_run() -> None:
    """Clear run identifiers bound by :func:`bind_run`."""
    structlog.contextvars.clear_contextvars()
