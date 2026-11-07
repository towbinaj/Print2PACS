"""Tests for echo + the convert-and-send pipeline (C-STORE)."""

from __future__ import annotations

from PIL import Image

from print2pacs.engine import reconcile, sc_import, send
from print2pacs.model import SeriesInfo
from print2pacs.net import scu


def test_echo(pacs):
    res = scu.c_echo("ME", pacs.node)
    assert res.success, res.message


def test_full_pipeline_reconcile_convert_send(pacs, tmp_path):
    # 1. reconcile
    ctx = reconcile.lookup_studies("ME", pacs.node, accession="ACC42")[0]
    # 2. convert
    pages = [sc_import.SourcePage(Image.new("RGB", (30, 20), (200, 10, 10)), "a"),
             sc_import.SourcePage(Image.new("L", (24, 16), 90), "b")]
    si = SeriesInfo(series_description="Pipeline Test", body_part="ABDOMEN")
    datasets = sc_import.convert_pages(pages, ctx, si)
    # 3. send
    result = send.send_series("ME", pacs.node, datasets, tmp_path / "job")
    assert result.sent == 2
    assert result.failed == 0
    assert len(pacs.received) == 2
    studies = {r.StudyInstanceUID for r in pacs.received}
    assert studies == {pacs.study_uid}       # attached to existing study
    series = {r.SeriesInstanceUID for r in pacs.received}
    assert len(series) == 1                   # one new series
    assert all(r.SOPClassUID == sc_import.SC_SOP_CLASS_UID
               for r in pacs.received)


def test_rle_compressed_pipeline_store(pacs, tmp_path):
    from pydicom.uid import RLELossless
    ctx = reconcile.lookup_studies("ME", pacs.node, accession="ACC1")[0]
    pages = [sc_import.SourcePage(Image.new("RGB", (40, 30), (10, 200, 10)), "a")]
    ds = sc_import.convert_pages(pages, ctx,
                                 SeriesInfo(series_description="rle"),
                                 compression="rle")
    assert ds[0].file_meta.TransferSyntaxUID == RLELossless
    result = send.send_series("ME", pacs.node, ds, tmp_path / "rle")
    assert result.sent == 1 and result.failed == 0
    assert pacs.received[0].file_meta.TransferSyntaxUID == RLELossless


def test_job_folder_cleaned_up(pacs, tmp_path):
    ctx = reconcile.lookup_studies("ME", pacs.node, accession="ACC1")[0]
    pages = [sc_import.SourcePage(Image.new("L", (8, 8), 1), "a")]
    datasets = sc_import.convert_pages(pages, ctx, SeriesInfo())
    job = tmp_path / "job"
    send.send_series("ME", pacs.node, datasets, job)
    assert not job.exists()       # cleanup=True by default
