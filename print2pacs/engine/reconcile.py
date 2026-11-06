"""Reconcile images to an existing PACS study via C-FIND.

Given an accession number (and/or patient ID), query the PACS at STUDY level and
return the matching studies as :class:`StudyContext` records. The user confirms
one; its ``study_uid`` is what the new Secondary Capture series attaches to.
"""

from __future__ import annotations

from typing import Callable

from pydicom.dataset import Dataset

from ..logging_setup import get_logger
from ..model import Node, StudyContext
from ..net import scu

log = get_logger("reconcile")

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _s(ds: Dataset, keyword: str) -> str:
    """Safely read a string attribute from a C-FIND result dataset."""
    val = getattr(ds, keyword, "")
    if val is None:
        return ""
    return str(val).strip()


def dataset_to_context(ds: Dataset) -> StudyContext:
    return StudyContext(
        patient_id=_s(ds, "PatientID"),
        patient_name=_s(ds, "PatientName"),
        birth_date=_s(ds, "PatientBirthDate"),
        sex=_s(ds, "PatientSex"),
        study_uid=_s(ds, "StudyInstanceUID"),
        study_date=_s(ds, "StudyDate"),
        study_time=_s(ds, "StudyTime"),
        accession=_s(ds, "AccessionNumber"),
        study_id=_s(ds, "StudyID"),
        study_description=_s(ds, "StudyDescription"),
        referring_physician=_s(ds, "ReferringPhysicianName"),
        modalities=_s(ds, "ModalitiesInStudy"),
    )


# Return keys requested from the PACS for each matching study.
_RETURN_KEYS = (
    "PatientID", "PatientName", "PatientBirthDate", "PatientSex",
    "StudyInstanceUID", "StudyDate", "StudyTime", "AccessionNumber",
    "StudyID", "StudyDescription", "ReferringPhysicianName",
    "ModalitiesInStudy",
)


class ReconcileError(Exception):
    pass


def lookup_studies(my_aetitle: str, node: Node, *, accession: str = "",
                   patient_id: str = "", progress: Progress = _noop,
                   tls_args=None) -> list[StudyContext]:
    """Query PACS for studies matching accession and/or patient ID."""
    accession = (accession or "").strip()
    patient_id = (patient_id or "").strip()
    if not accession and not patient_id:
        raise ReconcileError("Enter an accession number or a patient ID.")

    criteria = {k: "" for k in _RETURN_KEYS}
    if accession:
        criteria["AccessionNumber"] = accession
    if patient_id:
        criteria["PatientID"] = patient_id

    identifier = scu.build_query("STUDY", **criteria)
    result = scu.c_find(my_aetitle, node, identifier, model="STUDY",
                        progress=progress, tls_args=tls_args)
    if not result.datasets and not result.success:
        raise ReconcileError(result.message or "PACS query failed.")

    contexts = [dataset_to_context(ds) for ds in result.datasets]
    contexts = [c for c in contexts if c.study_uid]
    progress(f"Found {len(contexts)} matching study(ies).")
    return contexts
