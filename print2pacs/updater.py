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
from urllib.parse import urlparse

from . import __version__
from .logging_setup import get_logger

log = get_logger("updater")

OWNER = "towbinaj"
REPO = "Print2PACS"
ASSET_NAME = "Print2PACS.exe"
_API_LATEST = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
_HEADERS = {"User-Agent": f"Print2PACS/{__version__}",
            "Accept": "application/vnd.github+json"}

# Only these hosts may serve update metadata or assets. Enforced on the initial
# request *and* on every redirect, so a tampered API response cannot point the
# download at an arbitrary server.
_ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
# Hard ceiling for a downloaded asset (today's exe is ~120 MB).
_MAX_ASSET_BYTES = 400 * 1024 * 1024


def _check_https_host(url: str) -> None:
    """Raise unless ``url`` is HTTPS on an allow-listed GitHub host."""
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"Refusing non-allow-listed update URL: {url!r}")


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_https_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        _AllowlistRedirectHandler())


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
    _check_https_host(url)
    req = urllib.request.Request(url, headers=_HEADERS)
    with _opener().open(req, timeout=timeout) as r:
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
    """Download the new exe to ``dest`` and verify it.

    Fails closed: the download is rejected unless the release publishes a
    SHA-256 sidecar that matches the bytes received. URLs must be HTTPS on an
    allow-listed GitHub host (checked on redirects too), and the payload is
    capped at ``_MAX_ASSET_BYTES``.
    """
    dest = Path(dest)
    _check_https_host(info.url)
    if not info.sha256_url:
        raise ValueError(
            "Update rejected: release has no SHA-256 checksum to verify.")

    req = urllib.request.Request(info.url, headers=_HEADERS)
    hasher = hashlib.sha256()
    with _opener().open(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length", info.size or 0) or 0)
        done = 0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            done += len(chunk)
            if done > _MAX_ASSET_BYTES:
                dest.unlink(missing_ok=True)
                raise ValueError("Update rejected: asset exceeds size limit.")
            f.write(chunk)
            hasher.update(chunk)
            if progress and total:
                progress(done / total)

    try:
        expected = _get(info.sha256_url, 30).decode().split()[0].strip().lower()
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise ValueError(f"Update rejected: could not read checksum ({exc}).")
    if not expected:
        dest.unlink(missing_ok=True)
        raise ValueError("Update rejected: published checksum is empty.")
    if hasher.hexdigest() != expected:
        dest.unlink(missing_ok=True)
        raise ValueError("SHA-256 mismatch - download rejected.")
    return dest


def apply_update(info: UpdateInfo, progress=None) -> None:
    """Download the update and launch the swap helper; caller then exits."""
    if not can_self_update():
        raise RuntimeError("Self-update is only available in the built exe.")
    target = Path(sys.executable).resolve()
    # Unique, exclusively-created temp dir (0700) rather than a predictable path
    # another process could pre-plant and have us execute.
    tmp_dir = Path(tempfile.mkdtemp(prefix="Print2PACS-update-"))
    tmp = tmp_dir / f"Print2PACS-{info.version}.exe"
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
