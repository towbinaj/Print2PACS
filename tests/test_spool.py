"""Tests for the durable job spool / journal (crash + partial-failure recovery)."""

from __future__ import annotations

import socket
import types

import pytest
from PIL import Image
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.sop_class import SecondaryCaptureImageStorage

from print2pacs import paths
from print2pacs.engine import sc_import, spool
from print2pacs.model import Node, SeriesInfo, StudyContext
from print2pacs.net import scu

CTX = StudyContext(study_uid="1.2.3.4.5", patient_id="P1",
                   patient_name="DOE^JANE", accession="ACC1")


@pytest.fixture
def spool_dir(tmp_path, monkeypatch):
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr(paths, "jobs_dir", lambda: d)
    return d


def _datasets(n=2):
    pages = [sc_import.SourcePage(Image.new("L", (8, 8), i + 1), f"p{i}")
             for i in range(n)]
    return sc_import.convert_pages(pages, CTX, SeriesInfo(series_description="s"))


def _server(handler):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ae = AE(ae_title="SP")
    ae.add_supported_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)
    srv = ae.start_server(("127.0.0.1", port), block=False,
                          evt_handlers=[(evt.EVT_C_STORE, handler)])
    return srv, Node(name="n", aetitle="SP", host="127.0.0.1", port=port)


def test_classify_store_status():
    def st(code):
        return types.SimpleNamespace(Status=code)
    assert scu.classify_store_status(st(0x0000))[0] == "sent"
    assert scu.classify_store_status(st(0xB007))[0] == "warning"
    assert scu.classify_store_status(st(0xA700))[0] == "rejected"
    assert scu.classify_store_status(None)[0] == "uncertain"


def test_clean_send_resolves_and_discards(spool_dir):
    received = []

    def store(e):
        received.append(e.request.AffectedSOPInstanceUID)
        return 0x0000

    srv, node = _server(store)
    try:
        job = spool.prepare_job(_datasets(2), node, CTX)
        assert job.exists()
        out = spool.send_job(job, "ME", node)
        assert out.resolved and out.discarded
        assert out.store.sent == 2
        assert not job.exists()                 # removed once fully stored
        assert len(received) == 2
    finally:
        srv.shutdown()


def test_partial_failure_keeps_job_then_resume_reuses_uids(spool_dir):
    seen: set = set()
    received = []

    def store(e):
        uid = e.request.AffectedSOPInstanceUID
        received.append(uid)
        if uid in seen:
            return 0x0000                       # accept on retry
        seen.add(uid)
        return 0xA700                           # reject on first attempt

    srv, node = _server(store)
    try:
        job = spool.prepare_job(_datasets(3), node, CTX)
        out1 = spool.send_job(job, "ME", node)
        assert not out1.resolved
        assert out1.store.failed == 3
        assert out1.resumable == 3
        assert job.exists()                     # kept for retry

        out2 = spool.send_job(job, "ME", node)  # resume
        assert out2.resolved and out2.discarded
        # the SAME 3 UIDs were used both times -> no duplicate identities
        assert len(set(received)) == 3
        assert len(received) == 6
    finally:
        srv.shutdown()


def test_uncertain_objects_are_held_not_resent(spool_dir):
    received = []

    def store(e):
        received.append(e.request.AffectedSOPInstanceUID)
        return 0x0000

    srv, node = _server(store)
    try:
        job = spool.prepare_job(_datasets(2), node, CTX)
        # mark the first object uncertain, as if a prior send lost confirmation
        j = spool.read_journal(job)
        j["items"][0]["state"] = spool.UNCERTAIN
        spool._write(job, j)

        out = spool.send_job(job, "ME", node)
        assert not out.resolved                 # the uncertain one remains
        assert out.uncertain == 1
        assert out.resumable == 0
        assert len(received) == 1               # uncertain object NOT re-sent
        assert spool.read_journal(job)["items"][0]["state"] == spool.UNCERTAIN
    finally:
        srv.shutdown()


def test_unresolved_jobs_lists_only_incomplete(spool_dir):
    def store(e):
        return 0x0000

    srv, node = _server(store)
    try:
        job = spool.prepare_job(_datasets(1), node, CTX)
        assert [d for d, _ in spool.unresolved_jobs()] == [job]
        spool.send_job(job, "ME", node)         # clean -> discarded
        assert spool.unresolved_jobs() == []
    finally:
        srv.shutdown()
