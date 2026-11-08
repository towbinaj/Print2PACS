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


def test_tls_args_for_cleartext_and_tls():
    import ssl

    from print2pacs.model import Node

    plain = Node(name="p", aetitle="A", host="1.2.3.4", port=104, tls=False)
    assert scu.tls_args_for(plain) is None

    secure = Node(name="s", aetitle="A", host="pacs.example.org", port=2762,
                  tls=True)
    args = scu.tls_args_for(secure)
    assert args is not None
    ctx, host = args
    assert host == "pacs.example.org"
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True             # verifies hostname
    assert ctx.verify_mode == ssl.CERT_REQUIRED   # verifies certificate


def test_store_warning_counts_but_is_not_clean_success(tmp_path):
    """A store accepted *with a warning* must not read as a clean success."""
    import socket

    from pydicom.uid import ExplicitVRLittleEndian
    from pynetdicom import AE, evt
    from pynetdicom.sop_class import SecondaryCaptureImageStorage

    from print2pacs.model import Node, StudyContext

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    def handle_store(event):
        return 0xB007        # "Data Set does not match SOP Class" - a WARNING

    ae = AE(ae_title="WARNPACS")
    ae.add_supported_context(SecondaryCaptureImageStorage,
                             ExplicitVRLittleEndian)
    server = ae.start_server(("127.0.0.1", port), block=False,
                             evt_handlers=[(evt.EVT_C_STORE, handle_store)])
    try:
        node = Node(name="w", aetitle="WARNPACS", host="127.0.0.1", port=port)
        ctx = StudyContext(study_uid="1.2.3.4.5", patient_id="P1",
                           accession="A1")
        pages = [sc_import.SourcePage(Image.new("L", (8, 8), 1), "a")]
        datasets = sc_import.convert_pages(pages, ctx, SeriesInfo())
        result = send.send_series("ME", node, datasets, tmp_path / "job")
        assert result.sent == 1
        assert result.failed == 0
        assert result.warnings == 1
        assert result.warning_details           # detail is surfaced to the UI
        assert not result.clean                 # <-- not a clean success
    finally:
        server.shutdown()


def test_new_job_dir_is_unique_and_contained(tmp_path, monkeypatch):
    from print2pacs import paths

    monkeypatch.setattr(paths, "jobs_dir", lambda: tmp_path)
    a = paths.new_job_dir()
    b = paths.new_job_dir()
    assert a != b
    assert a.exists() and b.exists()
    assert a.parent == tmp_path and b.parent == tmp_path
    assert a.name.startswith("job_")
