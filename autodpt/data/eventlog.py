"""Event log (SRS DATA-04, NFR-LOG-01).

Timestamped (ISO 8601 with milliseconds), level, source, message. Sources are logger names:
``autodpt.gui``, ``autodpt.seq``, ``autodpt.hal.<instrument>``, ``autodpt.ana``. Every instrument
command/response is logged at DEBUG by the HAL (Phase 2); operator actions, state transitions and
safety-relevant commands at INFO.
"""

from __future__ import annotations

import datetime as dt
import logging
import logging.handlers
from pathlib import Path

ROOT = "autodpt"
_FMT = "%(asctime)s\t%(levelname)s\t%(name)s\t%(message)s"


class _IsoFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        t = dt.datetime.fromtimestamp(record.created).astimezone()
        return t.isoformat(timespec="milliseconds")


class MemoryHandler(logging.Handler):
    """Keeps records in memory so they can be written into the HDF5 run file (DATA-04)."""

    def __init__(self, capacity: int = 100_000) -> None:
        super().__init__(level=logging.DEBUG)
        self.capacity = capacity
        self.records: list[tuple[str, str, str, str]] = []
        self._fmt = _IsoFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.records) >= self.capacity:
            self.records.pop(0)
        self.records.append((self._fmt.formatTime(record), record.levelname, record.name,
                             record.getMessage()))


def setup_logging(log_dir: str | Path | None = None, console_level: int = logging.INFO,
                  file_level: int = logging.DEBUG, max_bytes: int = 10_000_000,
                  backups: int = 10) -> MemoryHandler:
    """Configure the ``autodpt`` logger tree. Returns the in-memory handler."""
    logger = logging.getLogger(ROOT)
    logger.setLevel(logging.DEBUG)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fmt = _IsoFormatter(_FMT)
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    if log_dir is not None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(d / "autodpt.log", maxBytes=max_bytes,
                                                  backupCount=backups, encoding="utf-8")
        fh.setLevel(file_level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    mem = MemoryHandler()
    logger.addHandler(mem)
    logger.propagate = False
    return mem


def get_logger(source: str) -> logging.Logger:
    """`source` e.g. 'seq', 'gui', 'hal.psu', 'ana'."""
    return logging.getLogger(f"{ROOT}.{source}")
