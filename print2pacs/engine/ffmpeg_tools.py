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

# Containment (R22): ffmpeg parses untrusted media, so restrict what it can do.
# 1) Only local-file protocols -> no network / external-URL references (a crafted
#    container cannot make ffmpeg phone home or pull in other files).
_PROTOCOL_WHITELIST = ["-protocol_whitelist", "file,crypto,data"]
# 2) Hard per-process memory ceiling for the ffmpeg child (Windows Job Object).
_FFMPEG_MEM_LIMIT = 4 * 1024 ** 3        # 4 GiB


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


def _loggable_args(args: list[str]) -> list[str]:
    """ffmpeg args with any filesystem path replaced by ``<path>``.

    A source filename can carry PHI (patient name / MRN), so it must never reach
    the log. The old filter only hid ``C:``/``/`` paths, leaking ``D:\\``, UNC
    (``\\\\server\\...``) and relative paths (RISK-004). This hides any argument
    that looks like a path: a separator, or a drive-letter prefix.
    """
    out: list[str] = []
    for a in args:
        if ("\\" in a) or ("/" in a) or (len(a) >= 2 and a[1] == ":"):
            out.append("<path>")
        else:
            out.append(a)
    return out


def _build_cmd(args: list[str]) -> list[str]:
    """Full ffmpeg argv. The protocol whitelist is an input option, so it must
    precede the first -i (it is placed up front, before any caller args)."""
    return [ensure_ffmpeg(), "-hide_banner", "-nostdin", *_PROTOCOL_WHITELIST,
            *args]


def run(args: list[str], *, input_bytes: bytes | None = None,
        timeout: int = 1800) -> subprocess.CompletedProcess:
    """Run ffmpeg with the given args (excluding the executable itself).

    ffmpeg is contained: a local-file-only protocol whitelist (no network /
    external references), and on Windows a Job Object with a hard memory cap that
    kills ffmpeg and any child processes when the job is closed (cancel/close).
    """
    cmd = _build_cmd(args)
    log.info("ffmpeg %s", " ".join(_loggable_args(args)))
    if sys.platform == "win32":
        return _run_contained_windows(cmd, input_bytes, timeout)
    return subprocess.run(
        cmd, input=input_bytes, capture_output=True,
        creationflags=_NO_WINDOW, timeout=timeout, check=False)


def _make_job():
    """Create a Windows Job Object with a memory cap + kill-on-close, or None."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            wintypes.LPVOID, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IOC(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong),
                    ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong),
                    ("e", ctypes.c_ulonglong), ("f", ctypes.c_ulonglong)]

    class _EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IOC),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    h = k32.CreateJobObjectW(None, None)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _EXT()
    # KILL_ON_JOB_CLOSE | LIMIT_PROCESS_MEMORY | LIMIT_ACTIVE_PROCESS
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0100 | 0x0008
    info.BasicLimitInformation.ActiveProcessLimit = 16
    info.ProcessMemoryLimit = _FFMPEG_MEM_LIMIT
    if not k32.SetInformationJobObject(h, 9, ctypes.byref(info),
                                       ctypes.sizeof(info)):
        err = ctypes.get_last_error()
        k32.CloseHandle(h)
        raise ctypes.WinError(err)
    return k32, h


def _run_contained_windows(cmd, input_bytes, timeout):
    """Run ffmpeg inside a Job Object; fall back to a plain run if that fails."""
    job = None
    try:
        job = _make_job()
    except Exception as exc:  # noqa: BLE001 - never let containment break ffmpeg
        log.warning("ffmpeg Job Object unavailable (%s); running uncontained.",
                    exc)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_bytes is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW, close_fds=True)
    except Exception:
        if job:
            job[0].CloseHandle(job[1])
        raise

    if job:
        try:
            job[0].AssignProcessToJobObject(job[1], int(proc._handle))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not assign ffmpeg to Job Object (%s).", exc)

    try:
        out, err = proc.communicate(input=input_bytes, timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        rc = proc.returncode
    finally:
        if job:
            job[0].CloseHandle(job[1])      # kill-on-close reaps any survivors
    return subprocess.CompletedProcess(cmd, rc, out, err)
