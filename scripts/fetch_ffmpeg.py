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

# Pinned BtbN FFmpeg build (GPL, win64). Update URL + SHA together on bump.
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/"
              "latest/ffmpeg-master-latest-win64-gpl.zip")
# Set to a known SHA-256 to enforce integrity; empty = accept + print it.
FFMPEG_SHA256 = ""

WANT = ("ffmpeg.exe",)   # ffmpeg only; metadata is probed via ffmpeg itself
DEST = Path(__file__).resolve().parent.parent / "assets" / "ffmpeg"


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    if all((DEST / w).exists() for w in WANT):
        print(f"ffmpeg already present in {DEST}")
        return 0

    print(f"Downloading {FFMPEG_URL} ...")
    data = urllib.request.urlopen(FFMPEG_URL, timeout=300).read()  # noqa: S310
    digest = hashlib.sha256(data).hexdigest()
    print(f"Downloaded {len(data) / 1e6:.1f} MB  sha256={digest}")
    if FFMPEG_SHA256 and digest != FFMPEG_SHA256:
        print("ERROR: SHA-256 mismatch", file=sys.stderr)
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
