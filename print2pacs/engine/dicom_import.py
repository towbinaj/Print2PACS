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

Identity consistency (why the re-filed object is unambiguously the target patient):
  * The top-level Patient/Study identity is overwritten from the looked-up study,
    AND the ORIGINAL patient's residual identifiers are removed -- issuer, other
    IDs/names, and any patient identity nested inside sequences -- so the object
    never carries two conflicting patient IDs. Links back into the original study
    (referenced/source image, study, series, PPS) are stripped because they would
    dangle on the target study; invalidated digital signatures are removed too.
    Clinical content (pixels, report text, measurements) is left intact.
  * Design decision -- demographics/alerts and private tags are RETAINED by
    design. This tool adds media to an *existing* study for the *same* patient;
    a dropped outside object is that same patient, and we only normalize the
    identity (name/MRN/issuer) to this site's values. So outside clinical/
    demographic attributes (PatientAge, PatientSize, PatientWeight, Allergies,
    MedicalAlerts, PregnancyStatus, ...) are legitimately that patient's and are
    deliberately KEPT -- losing them would be the real harm. Private/vendor tags
    are likewise kept by default (optional ``strip_private`` is available). The
    wrong-patient case is guarded at study *selection* (confirmation + pinned
    banner), not by deleting clinical fields here. Do NOT "harden" this by
    stripping demographics.
  * The on-disk file is hashed when it is added; if it changes before the send,
    the re-file is refused (the reviewed object must be the one that is sent).

Only DICOM objects go through this path. Images, PDFs and video are rasterized or
re-encoded to pixels elsewhere, which already discards anything embedded in the
original container.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
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


def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
    content_sha256: str = ""  # hash of the file bytes when it was added
    # The object's OWN patient identity as it arrived (before re-stamping), used
    # only to warn if it disagrees with the selected study (identity_mismatches).
    src_birth_date: str = ""
    src_sex: str = ""

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
        # defer_size: a bogus element length in a malformed/truncated file must
        # not make pydicom eagerly allocate a huge buffer during the preview read
        # (RISK-005). Large values are read lazily; we only touch small metadata.
        ds = dcmread(str(p), force=True, stop_before_pixels=True, defer_size=1024)
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
        content_sha256=_sha256(p),
        src_birth_date=str(getattr(ds, "PatientBirthDate", "") or ""),
        src_sex=str(getattr(ds, "PatientSex", "") or ""),
    )


# ---------------------------------------------------------------------------
# Identity cross-check (warn on a wrong-study drop)
# ---------------------------------------------------------------------------
@dataclass
class IdentityMismatch:
    """A field on a dropped object that disagrees with the target study."""

    field: str            # "Date of birth" / "Sex"
    source_value: str     # value the dropped object arrived with
    target_value: str     # value of the selected study


def _norm_dob(v: str) -> str:
    return "".join(ch for ch in (v or "") if ch.isdigit())


def _norm_sex(v: str) -> str:
    return (v or "").strip()[:1].upper()


def identity_mismatches(sources, context) -> list[IdentityMismatch]:
    """Distinct DOB/Sex disagreements between dropped objects and the study.

    Re-filing assumes the dropped object is the SAME patient; a differing date of
    birth or sex is a strong signal it is not (e.g. a wrong-study drop), so the
    operator is warned before it is attached (RISK-006 replacement safety stop).
    Only fields that BOTH sides actually carry are compared -- a blank on either
    side is no signal. MRN/name are intentionally not compared (they legitimately
    differ across institutions / by formatting).
    """
    out: list[IdentityMismatch] = []
    seen: set[tuple[str, str, str]] = set()
    t_dob, t_sex = _norm_dob(context.birth_date), _norm_sex(context.sex)
    for s in sources:
        checks = (
            ("Date of birth", _norm_dob(s.src_birth_date), t_dob,
             s.src_birth_date, context.birth_date),
            ("Sex", _norm_sex(s.src_sex), t_sex, s.src_sex, context.sex),
        )
        for field, sv, tv, raw_s, raw_t in checks:
            if sv and tv and sv != tv:
                key = (field, sv, tv)
                if key not in seen:
                    seen.add(key)
                    out.append(IdentityMismatch(field, raw_s or "—",
                                                raw_t or "—"))
    return out


# ---------------------------------------------------------------------------
# Re-stamping onto the target study
# ---------------------------------------------------------------------------
# Elements removed from the top level of a re-filed object. They either name the
# ORIGINAL patient (a second, conflicting identity) or point into the ORIGINAL
# study (links that resolve to nothing once the object is on the target study).
_STRIP_TOP_LEVEL = (
    # residual patient identity
    "IssuerOfPatientID", "IssuerOfPatientIDQualifiersSequence",
    "TypeOfPatientID", "OtherPatientIDs", "OtherPatientIDsSequence",
    "OtherPatientNames", "ReferencedPatientSequence",
    # stale order / accession identity
    "IssuerOfAccessionNumberSequence", "RequestAttributesSequence",
    # links into the original study (would dangle)
    "ReferencedStudySequence", "ReferencedSeriesSequence",
    "ReferencedImageSequence", "SourceImageSequence",
    "ReferencedInstanceSequence", "ReferencedPerformedProcedureStepSequence",
    # audit trail that can carry the old identity back in
    "OriginalAttributesSequence",
    # signatures invalidated by re-stamping
    "DigitalSignaturesSequence", "MACParametersSequence",
)

# Patient/accession identifiers purged wherever they appear in NESTED sequences.
_NESTED_IDENTITY = (
    "PatientID", "PatientName", "IssuerOfPatientID", "TypeOfPatientID",
    "OtherPatientIDs", "OtherPatientIDsSequence", "OtherPatientNames",
    "AccessionNumber",
)


def _scrub_nested_identity(ds) -> None:
    """Recursively delete foreign patient/accession identifiers from sequences."""
    for elem in ds:
        if elem.VR == "SQ":
            for item in elem.value:
                for kw in _NESTED_IDENTITY:
                    if kw in item:
                        del item[kw]
                _scrub_nested_identity(item)


def _scrub_foreign_identity(ds: FileDataset) -> None:
    """Remove the original patient's residual identity and stale study links.

    The correct top-level Patient/Study identity must already be set; this clears
    everything that would otherwise contradict it. Clinical content is untouched.
    """
    for kw in _STRIP_TOP_LEVEL:
        if kw in ds:
            del ds[kw]
    _scrub_nested_identity(ds)


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

    Patient + study identifiers are overwritten from ``context`` and the original
    patient's residual identity / stale study links are scrubbed, so the object
    unambiguously belongs to the target patient. A new series and SOP Instance UID
    are assigned. The object's own Modality, SOP Class, transfer syntax and pixel
    data are preserved. The preamble is zeroed and the File Meta rebuilt so the
    sent bytes are a clean pydicom reconstruction.
    """
    if not context.study_uid:
        raise ValueError("StudyContext.study_uid is required (reconcile first).")
    _ensure_valid(ds, "dropped object")

    # Declare UTF-8 for the whole dataset. The source was decoded on read using
    # its own SpecificCharacterSet, so the in-memory strings are already correct;
    # writing under ISO_IR 192 re-encodes them losslessly. Without this, a target
    # name with characters outside the source's charset (e.g. a Latin-1 source,
    # UTF-8 target name) would be silently corrupted to '?' on disk.
    ds.SpecificCharacterSet = "ISO_IR 192"

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

    # --- Identity consistency: purge the ORIGINAL patient's residual identity
    #     and stale links to the original study (top-level + nested). ---
    _scrub_foreign_identity(ds)

    # Stamp the TARGET assigning authority (the scrub removed the original's).
    if context.issuer_of_patient_id:
        ds.IssuerOfPatientID = context.issuer_of_patient_id

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
                   series_info: SeriesInfo, *, strip_private: bool = False,
                   start_series_number: int | None = None) -> list[FileDataset]:
    """Load dropped DICOM objects and re-file them onto the target study.

    Objects are grouped by (Modality, SOP Class) and each group becomes its own
    new series -- mixing, e.g., a US clip and a CR image into one series would be
    semantically wrong. Series numbers increment from ``series_info.series_number``
    (or ``start_series_number``). Each file is re-hashed and refused if it changed
    since it was added.
    """
    base_num = int(start_series_number if start_series_number is not None
                   else series_info.series_number)

    groups: dict[tuple[str, str], list[DicomSource]] = {}
    for src in sources:
        groups.setdefault((src.modality, src.sop_class_uid), []).append(src)

    out: list[FileDataset] = []
    for offset, grp in enumerate(groups.values()):
        series_uid = generate_uid()
        gi = replace(series_info, series_number=base_num + offset)
        for i, src in enumerate(grp, 1):
            name = Path(src.path).name
            if src.content_sha256 and _sha256(src.path) != src.content_sha256:
                raise NotDicom(
                    f"{name}: this file changed since it was added; "
                    "remove it and add it again.")
            try:
                ds = dcmread(str(src.path), force=True)
            except Exception as exc:  # noqa: BLE001
                raise NotDicom(
                    f"{name}: could not read DICOM ({exc}).") from exc
            _ensure_valid(ds, name)
            restamp(ds, context, gi, series_uid=series_uid,
                    instance_number=i, strip_private=strip_private)
            out.append(ds)
    log.info("Re-filed %d DICOM object(s) into %d series (strip_private=%s)",
             len(out), len(groups), strip_private)
    return out
