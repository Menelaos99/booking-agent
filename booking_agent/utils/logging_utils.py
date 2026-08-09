from __future__ import annotations

import contextvars
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

_indent_level: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_log_indent", default=0
)


class ColoredFormatter(logging.Formatter):
    """Compact formatter with TTY-aware warning and error colors."""

    LEVEL_COLORS: ClassVar[dict[int, str]] = {
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, *, use_color: bool | None = None) -> None:
        super().__init__()
        self.use_color = use_color if use_color is not None else sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        indent = "    " * _indent_level.get()
        message = record.getMessage()
        if record.levelno == logging.INFO:
            formatted = f"{indent}{message}"
        else:
            formatted = f"{indent}{record.levelname}: {message}"

        color = self.LEVEL_COLORS.get(record.levelno, "") if self.use_color else ""
        return f"{color}{formatted}{self.RESET}" if color else formatted


@contextmanager
def log_group(
    name: str,
    *,
    level: int = logging.INFO,
    logger: logging.Logger | None = None,
) -> Iterator[None]:
    """Log a batch item name and indent records emitted while processing it."""

    target = logger or logging.getLogger()
    target.log(level, name)
    token = _indent_level.set(_indent_level.get() + 1)
    try:
        yield
    finally:
        _indent_level.reset(token)


def setup_colored_logging(level: int = logging.INFO) -> None:
    """Install a compact colored handler on the root logger."""

    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
