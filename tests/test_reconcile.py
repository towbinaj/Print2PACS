"""Tests for the C-FIND reconcile step."""

from __future__ import annotations

import pytest
from pydicom.dataset import Dataset

from print2pacs.engine import reconcile


def test_dataset_to_context_maps_fields():
    ds = Dataset()
    ds.PatientID = "P9"
    ds.PatientName = "SMITH^JOHN^Q"
    ds.PatientBirthDate = "19551212"
    ds.PatientSex = "M"
    ds.StudyInstanceUID = "1.2.9"
    ds.StudyDate = "20200505"
    ds.AccessionNumber = "ACC55"
    ds.StudyDescription = "CT CHEST"
    ctx = reconcile.dataset_to_context(ds)
    assert ctx.patient_id == "P9"
    assert ctx.patient_name == "SMITH^JOHN^Q"
    assert ctx.birth_date == "19551212"
    assert ctx.sex == "M"
    assert ctx.study_uid == "1.2.9"
    assert ctx.accession == "ACC55"
    assert ctx.study_description == "CT CHEST"
    assert ctx.is_resolved


def test_lookup_requires_a_criterion():
    from print2pacs.model import Node
    node = Node(name="x", aetitle="A", host="127.0.0.1", port=1)
    with pytest.raises(reconcile.ReconcileError):
        reconcile.lookup_studies("ME", node, accession="", patient_id="")


def test_lookup_against_fake_pacs(pacs):
    contexts = reconcile.lookup_studies("ME", pacs.node, accession="ACC777")
    assert len(contexts) == 1
    c = contexts[0]
    assert c.study_uid == pacs.study_uid
    assert c.accession == "ACC777"       # echoed by the fake PACS
    assert c.patient_name == "DOE^JANE^A"
    assert c.is_resolved


# --------------------------------------------------------- query safety guards
def _find_server(handler):
    import socket

    from pynetdicom import AE, evt
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind

    from print2pacs.model import Node

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ae = AE(ae_title="FSCP")
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelFind)
    srv = ae.start_server(("127.0.0.1", port), block=False,
                          evt_handlers=[(evt.EVT_C_FIND, handler)])
    return srv, Node(name="f", aetitle="FSCP", host="127.0.0.1", port=port)


def _match(acc, uid):
    ds = Dataset()
    ds.QueryRetrieveLevel = "STUDY"
    ds.StudyInstanceUID = uid
    ds.AccessionNumber = acc
    ds.PatientID = "P"
    ds.PatientName = "DOE^JANE"
    return ds


def test_wildcard_only_query_is_rejected():
    from print2pacs.model import Node
    node = Node(name="x", aetitle="A", host="127.0.0.1", port=1)
    for acc, pid in (("*", ""), ("?*", ""), ("", "**")):
        with pytest.raises(reconcile.ReconcileError):
            reconcile.lookup_studies("ME", node, accession=acc, patient_id=pid)


def test_wildcard_in_accession_is_rejected():
    # RISK-006: the accession must be an exact match -- no wildcards allowed.
    from print2pacs.model import Node
    node = Node(name="x", aetitle="A", host="127.0.0.1", port=1)
    for acc in ("A*", "1*", "AC?"):
        with pytest.raises(reconcile.ReconcileError):
            reconcile.lookup_studies("ME", node, accession=acc)


def test_duplicate_accessions_helper():
    # RISK-006: flag an accession shared by more than one returned study.
    from print2pacs.model import StudyContext
    ctxs = [StudyContext(study_uid="1", accession="ACC1"),
            StudyContext(study_uid="2", accession="ACC1"),
            StudyContext(study_uid="3", accession="ACC2")]
    assert reconcile.duplicate_accessions(ctxs) == {"ACC1"}
    assert reconcile.duplicate_accessions([]) == set()


def test_partial_failure_result_is_rejected():
    def handler(event):
        yield 0xFF00, _match("ACC1", "1.2.3.4")   # a partial match ...
        yield 0xA700, None                         # ... then a failure status

    srv, node = _find_server(handler)
    try:
        with pytest.raises(reconcile.ReconcileError):
            reconcile.lookup_studies("ME", node, accession="ACC1")
    finally:
        srv.shutdown()


def test_too_many_matches_is_rejected():
    def handler(event):
        for i in range(120):
            yield 0xFF00, _match(f"A{i}", f"1.2.3.{i}")
        yield 0x0000, None

    srv, node = _find_server(handler)
    try:
        with pytest.raises(reconcile.ReconcileError):
            reconcile.lookup_studies("ME", node, patient_id="P", max_results=100)
    finally:
        srv.shutdown()


def test_dataset_to_context_parses_issuer():
    ds = Dataset()
    ds.PatientID = "MRN"
    ds.IssuerOfPatientID = "HOSP-A"
    ds.StudyInstanceUID = "1.2.3"
    ctx = reconcile.dataset_to_context(ds)
    assert ctx.issuer_of_patient_id == "HOSP-A"
