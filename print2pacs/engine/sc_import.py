"""Convert raster images and PDF pages into Secondary Capture DICOM objects.

The resulting datasets share one new Series Instance UID and are stamped onto an
*existing* study (``StudyContext.study_uid``) so PACS files them as a new series
under the original accession.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from ..logging_setup import get_logger
from ..model import SeriesInfo, StudyContext

log = get_logger("sc_import")

# Secondary Capture Image Storage SOP Class UID (PS3.6).
SC_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.7"

# Stable implementation identity for File Meta.
IMPLEMENTATION_CLASS_UID = "1.2.826.0.1.3680043.10.1480.1"  # org-neutral OID arc
IMPLEMENTATION_VERSION_NAME = "PRINT2PACS_0_1"

RASTER_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = RASTER_EXTS | PDF_EXTS

# Default DPI for rasterizing PDF pages.
PDF_RENDER_DPI = 200


class UnsupportedSource(Exception):
    pass


@dataclass
class SourcePage:
    """One page/frame loaded from a source file, ready to become one SC object."""

    image: Image.Image
    source: str           # original filename (for logging / display)
    page_index: int = 0   # 0-based page within a multi-page source


# ---------------------------------------------------------------------------
# Loading source files into pages
# ---------------------------------------------------------------------------
def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def load_pages(path: str | Path, pdf_dpi: int = PDF_RENDER_DPI) -> list[SourcePage]:
    """Load a source file into one or more :class:`SourcePage` objects."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in PDF_EXTS:
        return _load_pdf_pages(path, pdf_dpi)
    if ext in RASTER_EXTS:
        return _load_raster_pages(path)
    raise UnsupportedSource(f"Unsupported file type: {path.name}")


def _load_raster_pages(path: Path) -> list[SourcePage]:
    pages: list[SourcePage] = []
    with Image.open(path) as im:
        n = getattr(im, "n_frames", 1)
        for i in range(n):
            if n > 1:
                im.seek(i)
            frame = im.convert(im.mode)  # detach a copy from the file handle
            frame.load()
            pages.append(SourcePage(image=frame.copy(), source=path.name,
                                    page_index=i))
    return pages


def _load_pdf_pages(path: Path, dpi: int) -> list[SourcePage]:
    import fitz  # PyMuPDF
    pages: list[SourcePage] = []
    doc = fitz.open(str(path))
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(SourcePage(image=img, source=path.name, page_index=i))
    finally:
        doc.close()
    return pages


def load_many(paths: Iterable[str | Path],
              pdf_dpi: int = PDF_RENDER_DPI) -> list[SourcePage]:
    """Load several source files in order into a flat list of pages."""
    out: list[SourcePage] = []
    for p in paths:
        out.extend(load_pages(p, pdf_dpi))
    return out


# ---------------------------------------------------------------------------
# Pixel normalization
# ---------------------------------------------------------------------------
def _flatten_rgba(img: Image.Image) -> Image.Image:
    """Composite an image with alpha onto a white background -> RGB."""
    rgba = img.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.split()[-1])
    return bg


def _scale_to_8bit(img: Image.Image) -> Image.Image:
    """Map a >8-bit (or float) single-channel image to 8-bit grayscale by
    min/max window, so the detail is actually visible."""
    import numpy as np
    arr = np.asarray(img).astype("float64")
    mn, mx = float(arr.min()), float(arr.max())
    arr = (arr - mn) / (mx - mn) * 255.0 if mx > mn else np.zeros_like(arr)
    return Image.fromarray(arr.astype("uint8"))


def _normalize(img: Image.Image) -> tuple[Image.Image, str, int]:
    """Return (image, PhotometricInterpretation, SamplesPerPixel) as 8-bit.

    Color is preserved for color sources (incl. palette/CMYK/transparency);
    grayscale/bilevel stay MONOCHROME2; high-bit grayscale is windowed to 8-bit.
    """
    mode = img.mode
    if mode == "RGB":
        return img, "RGB", 3
    if mode == "L":
        return img, "MONOCHROME2", 1
    if mode == "1":
        return img.convert("L"), "MONOCHROME2", 1
    if mode == "P":
        # Palette may carry color and/or transparency - never flatten to gray.
        if "transparency" in img.info:
            return _flatten_rgba(img), "RGB", 3
        return img.convert("RGB"), "RGB", 3
    if mode == "PA":
        return _flatten_rgba(img), "RGB", 3
    if mode == "LA":
        la = img.convert("LA")
        bg = Image.new("L", la.size, 255)
        bg.paste(la.convert("L"), mask=la.split()[-1])
        return bg, "MONOCHROME2", 1
    if mode == "RGBA":
        return _flatten_rgba(img), "RGB", 3
    if mode in ("I", "I;16", "I;16B", "I;16L", "F"):
        return _scale_to_8bit(img), "MONOCHROME2", 1
    # CMYK, YCbCr, other color spaces -> RGB
    return img.convert("RGB"), "RGB", 3


# ---------------------------------------------------------------------------
# Building one Secondary Capture dataset
# ---------------------------------------------------------------------------
def build_sc_dataset(image: Image.Image, context: StudyContext,
                     series_info: SeriesInfo, *, series_uid: str,
                     instance_number: int,
                     when: datetime.datetime | None = None) -> FileDataset:
    """Create a single Secondary Capture Image dataset from a PIL image."""
    if not context.study_uid:
        raise ValueError("StudyContext.study_uid is required (reconcile first).")

    when = when or datetime.datetime.now()
    da = when.strftime("%Y%m%d")
    tm = when.strftime("%H%M%S")

    norm, photometric, spp = _normalize(image)
    cols, rows = norm.size
    pixel_bytes = norm.tobytes()

    sop_uid = generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SC_SOP_CLASS_UID
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = IMPLEMENTATION_CLASS_UID
    file_meta.ImplementationVersionName = IMPLEMENTATION_VERSION_NAME

    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\x00" * 128)
    # UTF-8 so international characters and emoji in names/descriptions are
    # represented correctly (Latin-1 cannot encode them).
    ds.SpecificCharacterSet = "ISO_IR 192"

    # --- Patient Module ---
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

    # --- General Series Module (NEW series) ---
    ds.Modality = series_info.modality or "OT"
    ds.SeriesInstanceUID = series_uid
    ds.SeriesNumber = int(series_info.series_number)
    ds.SeriesDescription = series_info.series_description or ""
    if series_info.body_part:
        ds.BodyPartExamined = series_info.body_part
    ds.SeriesDate = da
    ds.SeriesTime = tm

    # --- General Equipment Module (Manufacturer is Type 2) ---
    ds.Manufacturer = "Print2PACS"

    # --- SC Equipment Module ---
    ds.ConversionType = series_info.conversion_type or "WSD"
    ds.SecondaryCaptureDeviceManufacturer = "Print2PACS"

    # --- General Image Module ---
    ds.InstanceNumber = int(instance_number)
    ds.ImageType = ["DERIVED", "SECONDARY"]
    ds.ContentDate = da
    ds.ContentTime = tm

    # --- Image Pixel Module ---
    ds.SamplesPerPixel = spp
    ds.PhotometricInterpretation = photometric
    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    if spp == 3:
        ds.PlanarConfiguration = 0
    ds.PixelData = pixel_bytes

    # --- SOP Common ---
    ds.SOPClassUID = SC_SOP_CLASS_UID
    ds.SOPInstanceUID = sop_uid

    return ds


# ---------------------------------------------------------------------------
# Converting a batch of pages into a series of SC datasets
# ---------------------------------------------------------------------------
def convert_pages(pages: list[SourcePage], context: StudyContext,
                  series_info: SeriesInfo, *,
                  series_uid: str | None = None) -> list[FileDataset]:
    """Convert loaded pages into one coherent Secondary Capture series."""
    series_uid = series_uid or generate_uid()
    when = datetime.datetime.now()
    out: list[FileDataset] = []
    for i, page in enumerate(pages, 1):
        ds = build_sc_dataset(page.image, context, series_info,
                              series_uid=series_uid, instance_number=i,
                              when=when)
        out.append(ds)
    log.info("Converted %d page(s) to SC series %s", len(out), series_uid)
    return out


def convert_paths(paths: Iterable[str | Path], context: StudyContext,
                  series_info: SeriesInfo, *, pdf_dpi: int = PDF_RENDER_DPI,
                  series_uid: str | None = None) -> list[FileDataset]:
    """Load source files and convert them into a Secondary Capture series."""
    pages = load_many(paths, pdf_dpi)
    return convert_pages(pages, context, series_info, series_uid=series_uid)


def save_datasets(datasets: list[FileDataset], out_dir: str | Path) -> list[Path]:
    """Write datasets to ``out_dir`` as .dcm files; return the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for ds in datasets:
        dest = out_dir / f"{ds.SOPInstanceUID}.dcm"
        ds.save_as(str(dest), enforce_file_format=True)
        written.append(dest)
    return written
