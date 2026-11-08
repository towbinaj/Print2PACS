"""Minimal logging setup.

Tag values can be PHI; this logger is for app/activity events. Never write raw
pixel or full-header dumps here. As a backstop, a redaction filter scrubs DICOM
UIDs from every emitted record, and call sites avoid logging patient identifiers
or source file names.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from . import paths

_configured = False

# DICOM UID: a dotted run of 4+ numeric components (study/series/SOP UIDs).
_UID_RE = re.compile(r"\b\d+(?:\.\d+){3,}\b")


def redact_phi(text: str) -> str:
    """Replace DICOM UIDs in a log string with a placeholder."""
    return _UID_RE.sub("<uid>", str(text))


class _PHIRedactionFilter(logging.Filter):
    """Scrub UIDs from the fully-formatted message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break the app
            return True
        red = redact_phi(msg)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


def setup(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("print2pacs")
    root.setLevel(level)
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    redactor = _PHIRedactionFilter()

    try:
        fh = RotatingFileHandler(
            paths.logs_dir() / "print2pacs.log",
            maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.addFilter(redactor)
        root.addHandler(fh)
    except Exception:  # noqa: BLE001 - never let logging break the app
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.addFilter(redactor)
    root.addHandler(sh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"print2pacs.{name}")
