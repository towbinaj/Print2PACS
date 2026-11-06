"""Minimal logging setup.

Tag values can be PHI; this logger is for app/activity events. Never write raw
pixel or full-header dumps here.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import paths

_configured = False


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

    try:
        fh = RotatingFileHandler(
            paths.logs_dir() / "print2pacs.log",
            maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:  # noqa: BLE001 - never let logging break the app
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"print2pacs.{name}")
