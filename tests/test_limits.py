"""Tests for workload preflight limits (R05)."""

from __future__ import annotations

from PIL import Image

from print2pacs.engine import limits


def test_count_pages_pdf(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()
    p = tmp_path / "doc.pdf"
    doc.save(str(p))
    doc.close()
    assert limits.count_pages(p) == 3


def test_count_pages_multiframe_tiff(tmp_path):
    frames = [Image.new("L", (8, 8), v) for v in (10, 20, 30, 40)]
    p = tmp_path / "stack.tiff"
    frames[0].save(str(p), save_all=True, append_images=frames[1:])
    assert limits.count_pages(p) == 4


def test_count_pages_single_raster_and_unreadable(tmp_path):
    png = tmp_path / "a.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(str(png))
    assert limits.count_pages(png) == 1

    broken = tmp_path / "broken.pdf"      # not a real PDF -> counts as 1, no crash
    broken.write_bytes(b"not a pdf")
    assert limits.count_pages(broken) == 1


def test_pages_error():
    assert limits.pages_error(300, 300) is None      # at the limit is OK
    assert limits.pages_error(301, 300) is not None   # over -> message
    assert limits.pages_error(5000, 0) is None        # 0 disables the check


def test_video_duration_error():
    assert limits.video_duration_error(900, 900) is None
    assert limits.video_duration_error(901, 900) is not None
    assert limits.video_duration_error(0, 900) is None     # unknown duration
    assert limits.video_duration_error(99999, 0) is None   # disabled


def test_video_size_error():
    mb = 1024 * 1024
    assert limits.video_size_error(2048 * mb, 2048) is None
    assert limits.video_size_error(2048 * mb + 1, 2048) is not None
    assert limits.video_size_error(9 * 1024 * mb, 0) is None   # disabled
