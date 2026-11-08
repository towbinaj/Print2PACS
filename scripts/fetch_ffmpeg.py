"""Download static ffmpeg + ffprobe binaries for bundling.

Fetches a prebuilt Windows x64 ffmpeg (GPL build, includes libx264 for H.264)
and extracts just ffmpeg.exe and ffprobe.exe into ``assets/ffmpeg/``. That
folder is git-ignored; the PyInstaller spec bundles it, and CI runs this script
before building. Run once (or on version bump):

    .venv\\Scripts\\python scripts\\fetch_ffmpeg.py
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

# Pinned BtbN FFmpeg build (GPL, win64).
#
# This points at an *immutable, dated* BtbN autobuild release (NOT the rolling
# ``latest`` tag, whose asset is rebuilt daily under the same URL). FFMPEG_SHA256
# is mandatory and enforced: an empty or mismatched hash aborts the build rather
# than bundling an unverified binary. To update ffmpeg, pick a new dated release
# at https://github.com/BtbN/FFmpeg-Builds/releases and set BOTH URL and SHA-256
# together (the SHA-256 is shown as the asset ``digest`` in the GitHub API).
#
# NOTE: BtbN prunes old dated builds after a while. If this URL starts returning
# 404, CI will fail loudly (by design) — re-pin to a current dated build. The
# durable fix is to mirror the chosen artifact in hospital-controlled storage.
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/"
              "autobuild-2026-11-08-12-35/"
              "ffmpeg-N-128691-gcc583787bb-win64-gpl.zip")
FFMPEG_SHA256 = "33c5d91e11daaae7ad057b98216bb918182d80396f51891c539aa1ab15cc6aa1"

WANT = ("ffmpeg.exe",)   # ffmpeg only; metadata is probed via ffmpeg itself
DEST = Path(__file__).resolve().parent.parent / "assets" / "ffmpeg"


def main() -> int:
    if not FFMPEG_SHA256:
        print("ERROR: FFMPEG_SHA256 is not set; refusing to fetch an "
              "unverified binary.", file=sys.stderr)
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    if all((DEST / w).exists() for w in WANT):
        print(f"ffmpeg already present in {DEST}")
        return 0

    print(f"Downloading {FFMPEG_URL} ...")
    data = urllib.request.urlopen(FFMPEG_URL, timeout=300).read()  # noqa: S310
    digest = hashlib.sha256(data).hexdigest()
    print(f"Downloaded {len(data) / 1e6:.1f} MB  sha256={digest}")
    if digest != FFMPEG_SHA256:
        print(f"ERROR: SHA-256 mismatch\n  expected {FFMPEG_SHA256}\n  got      "
              f"{digest}", file=sys.stderr)
        return 1

    extracted = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            base = Path(info.filename).name
            if base in WANT:
                (DEST / base).write_bytes(zf.read(info))
                extracted += 1
                print(f"  extracted {base}")
    if extracted != len(WANT):
        print(f"ERROR: expected {WANT}, extracted {extracted}", file=sys.stderr)
        return 1
    print(f"Done -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
