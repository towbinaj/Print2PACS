"""Workload preflight limits (R05).

A job over any configured limit is refused -- with a clear message -- *before*
heavy conversion, so an oversized drop can't hang the app, exhaust memory, or
flood PACS. Counting is cheap: PDF/TIFF page counts and video duration are read
from metadata without decoding pixels. The encoded-video size is checked after
transcode (the only point it is known) and before the object is sent.

These are pure helpers (no GUI, no settings coupling): the caller passes the
configured maxima so the policy can be enforced and tested on its own.
"""

from __future__ import annotations

from pathlib import Path

_MB = 1024 * 1024


def count_pages(path: str | Path) -> int:
    """Pages/frames a source file would expand to, without decoding pixels.

    PDF page count and multi-frame TIFF frame count are read from the container
    metadata; any other raster is one page. Unreadable files count as 1 so the
    real loader surfaces the specific error later.
    """
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            import pymupdf
            doc = pymupdf.open(str(p))
            try:
                return int(doc.page_count)
            finally:
                doc.close()
        if ext in (".tif", ".tiff"):
            from PIL import Image
            with Image.open(p) as im:
                return int(getattr(im, "n_frames", 1))
    except Exception:  # noqa: BLE001 - counting must never crash the drop
        return 1
    return 1


def pages_error(total_pages: int, max_pages: int) -> str | None:
    if max_pages and total_pages > max_pages:
        return (f"This job has {total_pages} page(s), over the {max_pages}-page "
                "limit. Remove some items, or raise the limit in Settings.")
    return None


def video_duration_error(seconds: float, max_seconds: int) -> str | None:
    if max_seconds and seconds and seconds > max_seconds:
        return (f"This video is {seconds / 60:.1f} min, over the "
                f"{max_seconds // 60}-minute limit. Trim it, or raise the limit "
                "in Settings.")
    return None


def video_size_error(nbytes: int, max_mb: int) -> str | None:
    if max_mb and nbytes > max_mb * _MB:
        return (f"The converted video is {nbytes / 1e9:.2f} GB, over the "
                f"{max_mb} MB limit (a DICOM video object must stay under ~4 GB)."
                " Trim/compress it, or raise the limit in Settings.")
    return None
