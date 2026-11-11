"""Filesystem locations for the app's per-user data folder.

Settings/destinations/logs/jobs live in a per-user, owner-private folder
(``%LOCALAPPDATA%\\Print2PACS``, falling back to ``%APPDATA%`` then per-user
temp). They are deliberately NOT stored next to the executable: on a shared
clinical workstation that folder is readable by every user, so one operator's
spooled PHI would be exposed to the next (RISK-004). The admin destination
policy is the only file read from the install dir -- see ``config.policy_path``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
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


def exe_dir() -> Path:
    """The install directory that holds ``Print2PACS.exe`` (repo root from source).

    Unlike :func:`resource_dir`, this is NOT ``sys._MEIPASS`` when frozen: an
    admin file placed beside the shipped exe (e.g. the destination policy) must
    be resolved here, not in the ephemeral PyInstaller extraction folder.
    """
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Backwards-compatible internal alias.
_exe_dir = exe_dir


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
    """The per-user data folder, created on first use.

    Per-user, owner-private locations ONLY -- never beside the exe, which on a
    shared workstation is world-readable (RISK-004). ``%LOCALAPPDATA%`` and
    ``%APPDATA%`` are protected by per-user NTFS ACLs by default; the temp
    fallback is also per-user (under the user's profile).
    """
    global _cached_data_dir
    if _cached_data_dir is not None:
        return _cached_data_dir

    # Running from source (dev): keep the repo-local data folder for convenience.
    # This path is never the deployed one, so the shared-workstation risk below
    # does not apply.
    if not _frozen():
        beside = _exe_dir() / _DATA_DIRNAME
        if _writable(beside):
            _cached_data_dir = beside
            return beside

    # Frozen (deployed) build: per-user, owner-private locations ONLY -- never
    # beside the exe, which on a shared workstation is world-readable (RISK-004).
    for env in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            cand = Path(base) / APP_NAME
            if _writable(cand):
                _cached_data_dir = cand
                return cand

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


def new_job_dir() -> Path:
    """Create and return a unique, exclusively-created per-job workspace.

    The name is a random UUID, never derived from remote/PHI data such as the
    accession number: that avoids both path traversal (an accession could carry
    ``\\``, ``/`` or ``..``) and predictable-name collisions between concurrent
    jobs. Created with ``exist_ok=False`` so two jobs can never share a folder.
    """
    base = jobs_dir()
    for _ in range(8):
        d = base / f"job_{uuid.uuid4().hex}"
        try:
            d.mkdir(parents=False, exist_ok=False)
            return d
        except FileExistsError:
            continue
    raise OSError("Could not create a unique job directory.")


def settings_file() -> Path:
    return data_dir() / "settings.json"


def destinations_file() -> Path:
    return data_dir() / "destinations.json"
