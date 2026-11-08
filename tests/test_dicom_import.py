"""Tests for the DICOM passthrough / re-file engine (dicom_import)."""

from __future__ import annotations

import numpy as np
import pytest
from pydicom import dcmread
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from print2pacs.engine import dicom_import, reconcile, send
from print2pacs.model import SeriesInfo, StudyContext

US_SOP = "1.2.840.10008.5.1.4.1.1.6.1"      # Ultrasound Image Storage
CR_SOP = "1.2.840.10008.5.1.4.1.1.1"        # Computed Radiography Image Storage

TARGET = StudyContext(
    patient_id="NEW-001", patient_name="NEW^PATIENT", birth_date="19900202",
    sex="F", study_uid="1.2.3.4.5", study_date="20240101",
    accession="NEWACC", study_id="NS1", study_description="RE-FILED",
    referring_physician="REF^DOC")
SI = SeriesInfo(modality="OT", series_description="Re-filed", series_number=777)


def _make_source(path, *, with_private=True, polyglot=True, foreign=False,
                 modality="US", sop_class=US_SOP):
    """Write a minimal but valid DICOM instance, optionally with a PE-polyglot
    preamble (MZ header), a private tag block, and ``foreign`` residual identity
    (other IDs/names, issuer, stale references, nested patient ID) to simulate an
    outside object."""
    sop = generate_uid()
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = sop_class
    fm.MediaStorageSOPInstanceUID = sop
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    preamble = (b"MZ" + b"\x90" * 126) if polyglot else b"\x00" * 128
    ds = FileDataset(str(path), {}, file_meta=fm, preamble=preamble)
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.PatientName = "OLD^PATIENT"
    ds.PatientID = "OLDID"
    ds.PatientBirthDate = "19500101"
    ds.PatientSex = "M"
    ds.StudyInstanceUID = "1.2.999.1"
    ds.SeriesInstanceUID = "1.2.999.2"
    ds.SOPInstanceUID = sop
    ds.SOPClassUID = sop_class
    ds.Modality = modality
    ds.AccessionNumber = "OLDACC"
    ds.StudyID = "OLDSTID"
    ds.SeriesDescription = "ORIGINAL US"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = 8
    ds.Columns = 8
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = np.arange(64, dtype=np.uint8).tobytes()
    if foreign:
        ds.IssuerOfPatientID = "OLD-ISSUER"
        ds.OtherPatientIDs = "ALT-OLD-ID"
        ds.OtherPatientNames = "OLD^ALIAS"
        ref = Dataset()
        ref.ReferencedSOPClassUID = sop_class
        ref.ReferencedSOPInstanceUID = generate_uid()
        ds.SourceImageSequence = Sequence([ref])            # link to old study
        nested = Dataset()
        nested.PatientID = "NESTED-OLD"
        nested.AccessionNumber = "NESTED-OLDACC"
        ds.AdmittingDiagnosesCodeSequence = Sequence([nested])  # kept, scrubbed
    if with_private:
        block = ds.private_block(0x000b, "ACME 1.0", create=True)
        block.add_new(0x01, "LO", "sekret-blob")
    ds.save_as(str(path), enforce_file_format=True)
    return path, sop


# --------------------------------------------------------------- detection
def test_is_dicom_by_magic_regardless_of_extension(tmp_path):
    p = tmp_path / "object.txt"           # wrong extension on purpose
    _make_source(p)
    assert dicom_import.is_dicom(p)        # detected by DICM magic


def test_is_dicom_rejects_plain_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_bytes(b"not a dicom file at all")
    assert not dicom_import.is_dicom(p)


def test_load_dicom_summary(tmp_path):
    p, _ = _make_source(tmp_path / "a.dcm")
    src = dicom_import.load_dicom(p)
    assert src.modality == "US"
    assert src.sop_class_uid == US_SOP
    assert "Ultrasound" in src.sop_class_name
    assert "US" in src.label


# --------------------------------------------------------------- re-stamping
def test_restamp_morphs_identity_keeps_modality_and_sop_class(tmp_path):
    p, old_sop = _make_source(tmp_path / "a.dcm")
    [ds] = dicom_import.convert_dicoms([dicom_import.load_dicom(p)], TARGET, SI)

    # patient + study re-stamped to the target study
    assert str(ds.PatientName) == "NEW^PATIENT"
    assert ds.PatientID == "NEW-001"
    assert ds.PatientBirthDate == "19900202"
    assert ds.StudyInstanceUID == "1.2.3.4.5"
    assert ds.AccessionNumber == "NEWACC"
    assert ds.StudyDescription == "RE-FILED"

    # native identity preserved
    assert ds.Modality == "US"
    assert ds.SOPClassUID == US_SOP
    assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian

    # new series + new instance identity (no collision with the original)
    assert ds.SeriesInstanceUID != "1.2.999.2"
    assert ds.SeriesNumber == 777
    assert ds.SeriesDescription == "Re-filed"
    assert ds.SOPInstanceUID != old_sop
    assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID


def test_restamp_zeros_polyglot_preamble(tmp_path):
    # Source has an MZ (PE) header in its preamble; the re-filed object must not.
    p, _ = _make_source(tmp_path / "evil.dcm", polyglot=True)
    raw = p.read_bytes()
    assert raw[:2] == b"MZ"                      # the dropped file is a polyglot

    [ds] = dicom_import.convert_dicoms([dicom_import.load_dicom(p)], TARGET, SI)
    assert ds.preamble == b"\x00" * 128         # neutralized in memory

    out = tmp_path / "out.dcm"
    ds.save_as(str(out), enforce_file_format=True)
    saved = out.read_bytes()
    assert saved[:128] == b"\x00" * 128         # and on disk
    assert saved[128:132] == b"DICM"
    assert saved[:2] != b"MZ"


def test_private_tags_kept_by_default_stripped_on_request(tmp_path):
    p, _ = _make_source(tmp_path / "a.dcm", with_private=True)

    [kept] = dicom_import.convert_dicoms(
        [dicom_import.load_dicom(p)], TARGET, SI, strip_private=False)
    assert any(elem.tag.is_private for elem in kept)

    [stripped] = dicom_import.convert_dicoms(
        [dicom_import.load_dicom(p)], TARGET, SI, strip_private=True)
    assert not any(elem.tag.is_private for elem in stripped)
    # clinical pixel data survives the strip
    assert "PixelData" in stripped


def test_convert_many_shares_series_sequential_instances(tmp_path):
    srcs = []
    for i in range(3):
        p, _ = _make_source(tmp_path / f"s{i}.dcm")
        srcs.append(dicom_import.load_dicom(p))
    out = dicom_import.convert_dicoms(srcs, TARGET, SI)
    assert len({d.SeriesInstanceUID for d in out}) == 1      # one new series
    assert len({d.SOPInstanceUID for d in out}) == 3         # distinct instances
    assert [d.InstanceNumber for d in out] == [1, 2, 3]


def test_refile_scrubs_foreign_identity(tmp_path):
    p, _ = _make_source(tmp_path / "a.dcm", foreign=True)
    [ds] = dicom_import.convert_dicoms([dicom_import.load_dicom(p)], TARGET, SI)

    # object now shows ONLY the target patient
    assert ds.PatientID == "NEW-001"
    assert str(ds.PatientName) == "NEW^PATIENT"
    # residual foreign identity is gone
    assert "IssuerOfPatientID" not in ds
    assert "OtherPatientIDs" not in ds
    assert "OtherPatientNames" not in ds
    # stale links into the original study are removed
    assert "SourceImageSequence" not in ds
    # a kept sequence has its nested identity scrubbed
    assert "AdmittingDiagnosesCodeSequence" in ds
    item = ds.AdmittingDiagnosesCodeSequence[0]
    assert "PatientID" not in item
    assert "AccessionNumber" not in item
    # clinical pixels survive
    assert "PixelData" in ds


def test_heterogeneous_objects_split_into_separate_series(tmp_path):
    us, _ = _make_source(tmp_path / "us.dcm", modality="US", sop_class=US_SOP)
    cr, _ = _make_source(tmp_path / "cr.dcm", modality="CR", sop_class=CR_SOP)
    out = dicom_import.convert_dicoms(
        [dicom_import.load_dicom(us), dicom_import.load_dicom(cr)], TARGET, SI)
    assert len(out) == 2
    assert len({d.SeriesInstanceUID for d in out}) == 2      # one series each
    assert {int(d.SeriesNumber) for d in out} == {777, 778}  # sequential numbers
    assert {d.SOPClassUID for d in out} == {US_SOP, CR_SOP}  # modalities preserved


def test_refile_refused_if_file_changed_after_add(tmp_path):
    p, _ = _make_source(tmp_path / "a.dcm")
    src = dicom_import.load_dicom(p)
    p.write_bytes(p.read_bytes() + b"tampered")   # change the file after review
    with pytest.raises(dicom_import.NotDicom):
        dicom_import.convert_dicoms([src], TARGET, SI)


# --------------------------------------------------------------- send pipeline
def test_refile_pipeline_stores_onto_study(pacs, tmp_path):
    p, old_sop = _make_source(tmp_path / "a.dcm")
    ctx = reconcile.lookup_studies("ME", pacs.node, accession="ACC1")[0]
    datasets = dicom_import.convert_dicoms([dicom_import.load_dicom(p)], ctx, SI)

    result = send.send_series("ME", pacs.node, datasets, tmp_path / "job")
    assert result.sent == 1 and result.failed == 0
    assert len(pacs.received) == 1
    got = pacs.received[0]
    assert got.StudyInstanceUID == pacs.study_uid       # attached to the study
    assert got.SOPClassUID == US_SOP                    # modality preserved
    assert got.Modality == "US"
    assert got.SOPInstanceUID != old_sop                # fresh instance identity
