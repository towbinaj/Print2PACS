"""Self-update from GitHub Releases (mirrors the DICOM Toolkit mechanism).

Checks the repository's latest release and, for the built exe, downloads the new
``Print2PACS.exe`` (SHA-256 verified) and swaps it in. Only HTTPS **GET**
requests to GitHub are made - nothing is ever uploaded. The ``Print2PACS-data/``
folder beside the exe (settings, destinations, logs) is left untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .logging_setup import get_logger

log = get_logger("updater")

OWNER = "towbinaj"
REPO = "Print2PACS"
ASSET_NAME = "Print2PACS.exe"
_API_LATEST = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
_HEADERS = {"User-Agent": f"Print2PACS/{__version__}",
            "Accept": "application/vnd.github+json"}


@dataclass
class UpdateInfo:
    version: str          # e.g. "1.2.0"
    tag: str              # e.g. "v1.2.0"
    url: str              # exe download URL
    size: int
    sha256_url: str | None
    notes: str


def _parse_ver(s: str) -> tuple:
    out = []
    for part in str(s).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out or [0])


def is_newer(latest: str, current: str) -> bool:
    return _parse_ver(latest) > _parse_ver(current)


def current_version() -> str:
    return __version__


def can_self_update() -> bool:
    """Self-replacement only works for the built (frozen) exe."""
    return bool(getattr(sys, "frozen", False))


def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def check_latest(timeout: int = 15) -> UpdateInfo | None:
    """Return UpdateInfo if a newer release exists, else None. Never raises."""
    try:
        data = json.loads(_get(_API_LATEST, timeout))
    except Exception as exc:  # noqa: BLE001 - offline / no releases / rate limit
        log.info("Update check skipped: %s", exc)
        return None
    tag = str(data.get("tag_name", "") or "")
    if not tag or not is_newer(tag, __version__):
        return None
    exe_url = sha_url = None
    size = 0
    for a in data.get("assets", []) or []:
        name = a.get("name", "")
        if name == ASSET_NAME:
            exe_url = a.get("browser_download_url")
            size = int(a.get("size", 0) or 0)
        elif name == ASSET_NAME + ".sha256":
            sha_url = a.get("browser_download_url")
    if not exe_url:
        log.info("Release %s has no %s asset", tag, ASSET_NAME)
        return None
    return UpdateInfo(version=tag.lstrip("vV"), tag=tag, url=exe_url, size=size,
                      sha256_url=sha_url,
                      notes=str(data.get("body", "") or "")[:4000])


def download(info: UpdateInfo, dest: Path, progress=None,
             timeout: int = 300) -> Path:
    """Download the new exe to ``dest`` and verify its SHA-256 if published."""
    dest = Path(dest)
    req = urllib.request.Request(info.url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length", info.size or 0) or 0)
        done = 0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    if info.sha256_url:
        try:
            expected = _get(info.sha256_url, 30).decode().split()[0].strip().lower()
        except Exception:  # noqa: BLE001 - TLS already protected the transport
            expected = ""
        if expected:
            got = hashlib.sha256(dest.read_bytes()).hexdigest()
            if got != expected:
                dest.unlink(missing_ok=True)
                raise ValueError("SHA-256 mismatch - download rejected")
    return dest


def apply_update(info: UpdateInfo, progress=None) -> None:
    """Download the update and launch the swap helper; caller then exits."""
    if not can_self_update():
        raise RuntimeError("Self-update is only available in the built exe.")
    target = Path(sys.executable).resolve()
    tmp = Path(tempfile.gettempdir()) / f"Print2PACS-{info.version}.exe"
    download(info, tmp, progress)
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED | NEW_GROUP
    subprocess.Popen([str(tmp), "apply-update", str(target), str(os.getpid())],
                     close_fds=True, creationflags=flags)


def run_apply_update(target: str, pid: int) -> int:
    """Helper mode (on the downloaded exe): swap it into ``target`` + relaunch."""
    import shutil
    import time

    target_path = Path(target)
    me = Path(sys.executable).resolve()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            shutil.copy2(me, target_path)
            break
        except OSError:
            time.sleep(0.5)
    else:
        return 1
    try:
        subprocess.Popen([str(target_path)], close_fds=True)
    except OSError:
        return 1
    return 0
