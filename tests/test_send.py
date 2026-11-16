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


def test_contexts_8bit_explicit_only_16bit_both(tmp_path):
    # RISK-007 + T05: a 16-bit uncompressed SOP class is offered with BOTH Explicit
    # and Implicit VR LE, but an 8-bit one is offered as Explicit VR LE ONLY -- so
    # 8-bit pixel data keeps VR OB end-to-end (Implicit VR reconstructs it as OW,
    # which strict archives reject). A compressed object is proposed as-is.
    from pydicom.uid import (ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                             RLELossless)
    from print2pacs.model import StudyContext

    ctx = StudyContext(study_uid="1.2.3.4.5", patient_id="P1", accession="A1")

    def sc_ctx(files):
        return next(c for c in scu._contexts_for_files(files)
                    if str(c.abstract_syntax) == str(sc_import.SC_SOP_CLASS_UID))

    eight = sc_import.save_datasets(
        sc_import.convert_pages(
            [sc_import.SourcePage(Image.new("L", (8, 8), 1), "a")],
            ctx, SeriesInfo()), tmp_path / "e8")
    assert {str(t) for t in sc_ctx(eight).transfer_syntax} == \
        {str(ExplicitVRLittleEndian)}               # 8-bit: Explicit only (OB)

    sixteen = sc_import.save_datasets(
        sc_import.convert_pages(
            [sc_import.SourcePage(Image.new("I", (8, 8), 1000), "b")],
            ctx, SeriesInfo()), tmp_path / "e16")
    ts16 = {str(t) for t in sc_ctx(sixteen).transfer_syntax}
    assert str(ExplicitVRLittleEndian) in ts16 and str(ImplicitVRLittleEndian) in ts16

    rle = sc_import.save_datasets(
        sc_import.convert_pages(
            [sc_import.SourcePage(Image.new("RGB", (8, 8), (1, 2, 3)), "c")],
            ctx, SeriesInfo(), compression="rle"), tmp_path / "r")
    assert {str(t) for t in sc_ctx(rle).transfer_syntax} == {str(RLELossless)}


def _implicit_only_scp(received):
    import socket
    from pydicom.uid import ImplicitVRLittleEndian
    from pynetdicom import AE, evt
    from pynetdicom.sop_class import SecondaryCaptureImageStorage
    from print2pacs.model import Node

    def handle_store(event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        received.append(ds)
        return 0x0000

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ae = AE(ae_title="IMPLPACS")
    ae.add_supported_context(SecondaryCaptureImageStorage, ImplicitVRLittleEndian)
    srv = ae.start_server(("127.0.0.1", port), block=False,
                          evt_handlers=[(evt.EVT_C_STORE, handle_store)])
    return srv, Node(name="i", aetitle="IMPLPACS", host="127.0.0.1", port=port)


def test_store_16bit_to_implicit_vr_only_scp(tmp_path):
    # RISK-007 (still applies to 16-bit): an Implicit-VR-only SCP receives our
    # 16-bit object, reconstructed as VR OW -- which is correct for BitsAllocated=16.
    from pydicom.uid import ImplicitVRLittleEndian
    from print2pacs.model import StudyContext

    received = []
    srv, node = _implicit_only_scp(received)
    try:
        ctx = StudyContext(study_uid="1.2.3.4.5", patient_id="P1", accession="A1")
        datasets = sc_import.convert_pages(
            [sc_import.SourcePage(Image.new("I", (8, 8), 1000), "a")],
            ctx, SeriesInfo())
        assert datasets[0].BitsAllocated == 16
        result = send.send_series("ME", node, datasets, tmp_path / "impl16")
        assert result.sent == 1 and result.failed == 0
        assert received[0].file_meta.TransferSyntaxUID == ImplicitVRLittleEndian
        assert received[0]["PixelData"].VR == "OW"      # 16-bit -> OW (consistent)
    finally:
        srv.shutdown()


def test_8bit_not_sent_to_implicit_only_scp(tmp_path):
    # T05: an 8-bit object is never offered over Implicit VR, so an Implicit-only
    # SCP cannot receive it -- a clean failure (nothing stored), never a silently
    # mislabeled (OW) 8-bit object.
    from print2pacs.model import StudyContext

    received = []
    srv, node = _implicit_only_scp(received)
    try:
        ctx = StudyContext(study_uid="1.2.3.4.5", patient_id="P1", accession="A1")
        datasets = sc_import.convert_pages(
            [sc_import.SourcePage(Image.new("L", (8, 8), 7), "a")],
            ctx, SeriesInfo())
        result = send.send_series("ME", node, datasets, tmp_path / "impl8")
        assert result.sent == 0                         # no common context -> unsent
        assert received == []
    finally:
        srv.shutdown()


def test_new_job_dir_is_unique_and_contained(tmp_path, monkeypatch):
    from print2pacs import paths

    monkeypatch.setattr(paths, "jobs_dir", lambda: tmp_path)
    a = paths.new_job_dir()
    b = paths.new_job_dir()
    assert a != b
    assert a.exists() and b.exists()
    assert a.parent == tmp_path and b.parent == tmp_path
    assert a.name.startswith("job_")
