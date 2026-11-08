"""Locate and run the bundled (or system) ffmpeg binary."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .. import paths
from ..logging_setup import get_logger

log = get_logger("ffmpeg")

# Hide the console window when ffmpeg runs under the GUI on Windows.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_EXE = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"


class FFmpegNotFound(Exception):
    pass


def ffmpeg_path() -> str | None:
    """Return the ffmpeg executable path.

    The bundled binary is always preferred. In the built (frozen) exe we never
    fall back to a PATH-resolved ffmpeg: a managed install ships its own verified
    binary, and running whatever ``ffmpeg`` happens to be on PATH would let a
    planted executable run with the user's rights (R22). From source, PATH
    fallback stays for developer convenience.
    """
    bundled = paths.resource_dir() / "assets" / "ffmpeg" / _EXE
    if bundled.exists():
        return str(bundled)
    if getattr(sys, "frozen", False):
        return None
    return shutil.which("ffmpeg")


def have_ffmpeg() -> bool:
    return ffmpeg_path() is not None


def ensure_ffmpeg() -> str:
    path = ffmpeg_path()
    if not path:
        raise FFmpegNotFound(
            "ffmpeg was not found. Run scripts/fetch_ffmpeg.py, or install "
            "ffmpeg and ensure it is on PATH.")
    return path


def run(args: list[str], *, input_bytes: bytes | None = None,
        timeout: int = 1800) -> subprocess.CompletedProcess:
    """Run ffmpeg with the given args (excluding the executable itself)."""
    cmd = [ensure_ffmpeg(), "-hide_banner", "-nostdin", *args]
    log.info("ffmpeg %s", " ".join(a for a in args if not a.startswith(("C:", "/"))))
    return subprocess.run(
        cmd, input=input_bytes, capture_output=True,
        creationflags=_NO_WINDOW, timeout=timeout, check=False)
