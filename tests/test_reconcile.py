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
