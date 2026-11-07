"""Accept existing DICOM objects and re-file them onto a looked-up study.

A dropped DICOM instance is re-stamped with the *target* study's patient/study
identity (from C-FIND), given a brand-new series + SOP Instance UID, and then
re-serialized through pydicom so PACS files it as a new series on the existing
study -- the same end result as the image/PDF path, but keeping the object's
native pixels, Modality and SOP Class instead of flattening to Secondary Capture.

Safety (why re-filing a foreign DICOM is not an entrypoint):
  * Every object is parsed and **rebuilt by pydicom**; the bytes we send are
    regenerated, never the dropped file verbatim.
  * The 128-byte file *preamble* is zeroed. That unused region is the vehicle for
    the known "DICOM-as-PE" polyglot (CVE-2019-11687): a file that is both a
    valid .dcm and a runnable Windows .exe. Zeroing it removes the executable
    header, so a re-filed object cannot double as a launchable binary.
  * The object is validated as a real DICOM instance (SOP Class + readable
    dataset) before anything is sent.
  * Private/vendor tags can optionally be stripped (``strip_private``) for sites
    that do not want opaque vendor blobs forwarded into the archive.

Only DICOM objects go through this path. Images, PDFs and video are rasterized or
re-encoded to pixels elsewhere, which already discards anything embedded in the
original container.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydicom import dcmread
from pydicom.dataset import FileDataset
from pydicom.uid import UID, generate_uid

from ..logging_setup import get_logger
from ..model import SeriesInfo, StudyContext
from .sc_import import IMPLEMENTATION_CLASS_UID, IMPLEMENTATION_VERSION_NAME

log = get_logger("dicom_import")

DICOM_EXTS = {".dcm", ".dicom", ".ima"}
_MAGIC = b"DICM"
_PREAMBLE = b"\x00" * 128


class NotDicom(Exception):
    """Raised when a file is not a usable DICOM object."""


# ---------------------------------------------------------------------------
# Detection + loading
# ---------------------------------------------------------------------------
def is_dicom(path: str | Path) -> bool:
    """True if ``path`` is a DICOM Part-10 object.

    Detection is by content (the ``DICM`` magic at byte offset 128), so a DICOM
    file is recognized regardless of its extension; a .dcm/.dicom/.ima extension
    is accepted as a fallback for preamble-less files.
    """
    p = Path(path)
    try:
        with open(p, "rb") as f:
            head = f.read(132)
    except OSError:
        return False
    if len(head) >= 132 and head[128:132] == _MAGIC:
        return True
    return p.suffix.lower() in DICOM_EXTS


@dataclass
class DicomSource:
    """One dropped DICOM instance, summarized for the review list."""

    path: Path
    sop_class_uid: str
    modality: str
    description: str          # original SeriesDescription (for display)
    nframes: int = 1

    @property
    def sop_class_name(self) -> str:
        try:
            name = UID(self.sop_class_uid).name
        except Exception:  # noqa: BLE001
            name = ""
        if not name or name == self.sop_class_uid:
            return "DICOM object"
        # "Ultrasound Multi-frame Image Storage" -> "Ultrasound Multi-frame Image"
        return name.replace(" Storage", "").strip() or "DICOM object"

    @property
    def label(self) -> str:
        bits = [self.sop_class_name]
        if self.modality:
            bits.append(self.modality)
        if self.nframes and self.nframes > 1:
            bits.append(f"{self.nframes} frames")
        if self.description:
            bits.append(self.description)
        return " · ".join(bits)


def load_dicom(path: str | Path) -> DicomSource:
    """Read just enough metadata to show a dropped DICOM in the review list."""
    p = Path(path)
    try:
        ds = dcmread(str(p), force=True, stop_before_pixels=True)
    except Exception as exc:  # noqa: BLE001
        raise NotDicom(f"{p.name}: not a readable DICOM object ({exc}).") from exc
    sop = str(getattr(ds, "SOPClassUID", "") or "")
    if not sop:
        raise NotDicom(f"{p.name}: not a DICOM object (no SOP Class UID).")
    try:
        nframes = int(getattr(ds, "NumberOfFrames", 1) or 1)
    except (TypeError, ValueError):
        nframes = 1
    return DicomSource(
        path=p,
        sop_class_uid=sop,
        modality=str(getattr(ds, "Modality", "") or ""),
        description=str(getattr(ds, "SeriesDescription", "") or ""),
        nframes=nframes,
    )


# ---------------------------------------------------------------------------
# Re-stamping onto the target study
# ---------------------------------------------------------------------------
def _ensure_valid(ds: FileDataset, name: str) -> None:
    if not getattr(ds, "SOPClassUID", None):
        raise NotDicom(f"{name}: missing SOP Class UID; not a storable object.")
    if not getattr(ds, "file_meta", None) or not getattr(
            ds.file_meta, "TransferSyntaxUID", None):
        raise NotDicom(f"{name}: missing transfer syntax; cannot re-file safely.")


def restamp(ds: FileDataset, context: StudyContext, series_info: SeriesInfo, *,
            series_uid: str, instance_number: int,
            strip_private: bool = False) -> FileDataset:
    """Re-file ``ds`` onto ``context``'s study as a new series, in place.

    Patient + study identifiers are overwritten from ``context``; a new series
    and SOP Instance UID are assigned. The object's own Modality, SOP Class,
    transfer syntax and pixel data are preserved. The preamble is zeroed and the
    File Meta rebuilt so the sent bytes are a clean pydicom reconstruction.
    """
    if not context.study_uid:
        raise ValueError("StudyContext.study_uid is required (reconcile first).")
    _ensure_valid(ds, "dropped object")

    # --- Patient Module (overwrite to match the archived study) ---
    ds.PatientName = context.patient_name or ""
    ds.PatientID = context.patient_id or ""
    ds.PatientBirthDate = context.birth_date or ""
    ds.PatientSex = context.sex or ""

    # --- General Study Module (attach to the EXISTING study) ---
    ds.StudyInstanceUID = context.study_uid
    ds.StudyDate = context.study_date or ""
    ds.StudyTime = context.study_time or ""
    ds.AccessionNumber = context.accession or ""
    ds.StudyID = context.study_id or ""
    ds.StudyDescription = context.study_description or ""
    ds.ReferringPhysicianName = context.referring_physician or ""

    # --- General Series Module (NEW series; keep the object's own Modality) ---
    new_sop = generate_uid()
    ds.SeriesInstanceUID = series_uid
    ds.SeriesNumber = int(series_info.series_number)
    if series_info.series_description:
        ds.SeriesDescription = series_info.series_description
    if series_info.body_part:
        ds.BodyPartExamined = series_info.body_part
    ds.InstanceNumber = int(instance_number)

    # --- Security: optionally drop opaque vendor data ---
    if strip_private:
        ds.remove_private_tags()

    # --- SOP Common (new identity so it cannot collide with the original) ---
    ds.SOPInstanceUID = new_sop

    # --- Rebuild File Meta + neutralize the preamble (anti-polyglot) ---
    fm = ds.file_meta
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = new_sop
    fm.ImplementationClassUID = IMPLEMENTATION_CLASS_UID
    fm.ImplementationVersionName = IMPLEMENTATION_VERSION_NAME
    # Keep the original TransferSyntaxUID: the pixel data is encoded for it.
    ds.preamble = _PREAMBLE
    return ds


def convert_dicoms(sources: list[DicomSource], context: StudyContext,
                   series_info: SeriesInfo, *, series_uid: str | None = None,
                   strip_private: bool = False,
                   start_number: int = 1) -> list[FileDataset]:
    """Load dropped DICOM objects and re-file them as one new series."""
    series_uid = series_uid or generate_uid()
    out: list[FileDataset] = []
    for i, src in enumerate(sources, start_number):
        try:
            ds = dcmread(str(src.path), force=True)
        except Exception as exc:  # noqa: BLE001
            raise NotDicom(
                f"{Path(src.path).name}: could not read DICOM ({exc}).") from exc
        _ensure_valid(ds, Path(src.path).name)
        restamp(ds, context, series_info, series_uid=series_uid,
                instance_number=i, strip_private=strip_private)
        out.append(ds)
    log.info("Re-filed %d DICOM object(s) into series %s (strip_private=%s)",
             len(out), series_uid, strip_private)
    return out
