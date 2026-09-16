"""
Debug logging for vyctl.

Off by default.  When enabled, a rotating log lands next to ``config.json`` and records
the things that are actually hard to reconstruct after the fact: which console got which
pid, the exact startup command typed into each shell, status transitions, teardown, and
any exception a reader task swallowed.

Two levels of detail:

* ``log_level: "info" | "debug"`` -- lifecycle and errors.
* ``log_raw_stream: true``       -- additionally dumps every byte the consoles emit.
  That is the tool for chasing a rendering bug (it is how the mis-parsed ``CSI > 4 ; 2 m``
  was found), but the dump contains the full text of your sessions, so it is opt-in and
  should be turned back off afterwards.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

#: Logger every module in the package writes to.
LOGGER_NAME = "vyctl"

#: Separate logger for the raw console byte stream, so it can be silenced on its own.
RAW_LOGGER_NAME = "vyctl.raw"

_LEVELS = {
    "off": None,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

#: Set once :func:`setup` has run, so callers can cheaply skip expensive formatting.
_active = False
_raw_active = False


def log() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def is_active() -> bool:
    return _active


def raw_active() -> bool:
    return _raw_active


def setup(
    directory: Path,
    level: str = "off",
    raw_stream: bool = False,
    filename: str = "vyctl.log",
) -> Path | None:
    """Configure logging.  Returns the log path, or ``None`` when disabled."""
    global _active, _raw_active

    resolved = _LEVELS.get((level or "off").lower(), None)
    logger = logging.getLogger(LOGGER_NAME)
    raw_logger = logging.getLogger(RAW_LOGGER_NAME)

    # Always start from a clean slate so a re-setup cannot double-log.
    for target in (logger, raw_logger):
        for handler in list(target.handlers):
            target.removeHandler(handler)
            handler.close()

    if resolved is None:
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        _active = _raw_active = False
        return None

    path = directory / filename
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8", delay=True
        )
    except OSError:
        logger.addHandler(logging.NullHandler())
        _active = _raw_active = False
        return None

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    )
    logger.setLevel(resolved)
    logger.addHandler(handler)
    logger.propagate = False

    # The raw stream shares the file but is gated separately.
    raw_logger.setLevel(logging.DEBUG if raw_stream else logging.CRITICAL + 1)
    raw_logger.propagate = True

    _active = True
    _raw_active = bool(raw_stream)
    return path


def raw(session_name: str, data: str) -> None:
    """Record a chunk of console output verbatim (only when raw logging is on)."""
    if not _raw_active:
        return
    logging.getLogger(RAW_LOGGER_NAME).debug("%s <- %r", session_name, data)
