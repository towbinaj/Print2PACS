"""Filesystem locations for the app's portable data folder.

Settings/destinations/logs live in a ``Print2PACS-data`` folder next to the
executable (or the repo root when run from source). If that location is not
writable, fall back to ``%APPDATA%\\Print2PACS``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from . import APP_NAME

_DATA_DIRNAME = f"{APP_NAME}-data"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Directory holding bundled resources (assets, etc.)."""
    if _frozen():
        # PyInstaller onefile extracts to sys._MEIPASS.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _exe_dir() -> Path:
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


_cached_data_dir: Path | None = None


def data_dir() -> Path:
    """The portable data folder, created on first use."""
    global _cached_data_dir
    if _cached_data_dir is not None:
        return _cached_data_dir

    beside = _exe_dir() / _DATA_DIRNAME
    if _writable(beside):
        _cached_data_dir = beside
        return beside

    appdata = os.environ.get("APPDATA")
    if appdata:
        fallback = Path(appdata) / APP_NAME
        if _writable(fallback):
            _cached_data_dir = fallback
            return fallback

    tmp = Path(tempfile.gettempdir()) / APP_NAME
    tmp.mkdir(parents=True, exist_ok=True)
    _cached_data_dir = tmp
    return tmp


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def jobs_dir() -> Path:
    """Temporary workspace for converted DICOM files before sending."""
    d = data_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_file() -> Path:
    return data_dir() / "settings.json"


def destinations_file() -> Path:
    return data_dir() / "destinations.json"
