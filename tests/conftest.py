"""Shared test fixtures: a throwaway localhost PACS (Storage + Find + Echo)."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

import pytest
from pydicom.dataset import Dataset
from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind, Verification)

from print2pacs.model import Node


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@dataclass
class FakePACS:
    node: Node
    received: list = field(default_factory=list)
    # Canned study returned by C-FIND; echoes back the queried accession.
    study_uid: str = "1.2.3.4.5.6.7.8.9.100"


@pytest.fixture
def pacs():
    port = _free_port()
    state = FakePACS(node=Node(name="t", aetitle="TESTPACS",
                              host="127.0.0.1", port=port))

    def handle_store(event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        state.received.append(ds)
        return 0x0000

    def handle_find(event):
        req = event.identifier
        acc = getattr(req, "AccessionNumber", "") or ""
        pid = getattr(req, "PatientID", "") or ""
        ds = Dataset()
        ds.QueryRetrieveLevel = "STUDY"
        ds.PatientID = pid or "TEST123"
        ds.PatientName = "DOE^JANE^A"
        ds.PatientBirthDate = "19700101"
        ds.PatientSex = "F"
        ds.StudyInstanceUID = state.study_uid
        ds.StudyDate = "20240101"
        ds.StudyTime = "120000"
        ds.AccessionNumber = acc or "ACC999"
        ds.StudyID = "S1"
        ds.StudyDescription = "TEST STUDY"
        ds.ReferringPhysicianName = "SMITH^BOB"
        ds.ModalitiesInStudy = "MR"
        yield 0xFF00, ds
        yield 0x0000, None

    def handle_echo(event):
        return 0x0000

    from print2pacs.engine.video import (VIDEO_SOP_CLASS_UID,
                                         H264_TRANSFER_SYNTAX)
    ae = AE(ae_title="TESTPACS")
    for cx in StoragePresentationContexts:
        ae.add_supported_context(cx.abstract_syntax, cx.transfer_syntax)
    # Accept encapsulated H.264 video too.
    ae.add_supported_context(VIDEO_SOP_CLASS_UID, H264_TRANSFER_SYNTAX)
    # Accept RLE-compressed Secondary Capture.
    from pydicom.uid import RLELossless
    ae.add_supported_context("1.2.840.10008.5.1.4.1.1.7", RLELossless)
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelFind)
    ae.add_supported_context(Verification)
    server = ae.start_server(
        ("127.0.0.1", port), block=False,
        evt_handlers=[(evt.EVT_C_STORE, handle_store),
                      (evt.EVT_C_FIND, handle_find),
                      (evt.EVT_C_ECHO, handle_echo)])
    try:
        yield state
    finally:
        server.shutdown()
