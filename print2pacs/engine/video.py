"""Convert video files into DICOM Video Photographic Image Storage objects.

DICOM does not store AVI/MP4/MOV containers; it carries an encapsulated H.264
(or MPEG-2/HEVC) elementary stream. Here we transcode any common video to a
conformant **MPEG-4 AVC/H.264 High Profile / Level 4.1** stream and encapsulate
it, stamped onto the existing study as a new series.
"""

from __future__ import annotations

import datetime
import io
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.tag import Tag
from pydicom.uid import generate_uid

from ..logging_setup import get_logger
from ..model import SeriesInfo, StudyContext
from . import ffmpeg_tools
from .sc_import import (IMPLEMENTATION_CLASS_UID, IMPLEMENTATION_VERSION_NAME)

log = get_logger("video")

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".mpg",
              ".mpeg", ".webm", ".ts", ".flv"}

# Video Photographic Image Storage (PS3.6).
VIDEO_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.77.1.4.1"
# MPEG-4 AVC/H.264 High Profile / Level 4.1 (PS3.5).
H264_TRANSFER_SYNTAX = "1.2.840.10008.1.2.4.102"

FrameTimeTag = Tag(0x0018, 0x1063)


class VideoError(Exception):
    pass


@dataclass
class VideoMeta:
    width: int
    height: int
    fps: float
    nframes: int
    duration: float = 0.0


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


# ---------------------------------------------------------------------------
# Probe (via ffmpeg -i stderr parsing; avoids bundling ffprobe)
# ---------------------------------------------------------------------------
_RES_RE = re.compile(r",\s*(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe(path: str | Path) -> VideoMeta:
    path = Path(path)
    proc = ffmpeg_tools.run(["-i", str(path)], timeout=120)
    text = proc.stderr.decode("utf-8", "replace")

    # Prefer the video stream line (the one carrying a resolution).
    width = height = 0
    fps = 0.0
    for line in text.splitlines():
        if "Video:" in line:
            m = _RES_RE.search(line)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
            f = _FPS_RE.search(line)
            if f:
                fps = float(f.group(1))
            break
    duration = 0.0
    d = _DUR_RE.search(text)
    if d:
        duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))

    if not (width and height):
        raise VideoError(f"Could not read video dimensions from {path.name}.")
    if fps <= 0:
        fps = 25.0  # sane default if ffmpeg didn't report it
    nframes = int(round(duration * fps)) if duration else 0
    return VideoMeta(width, height, fps, nframes, duration)


def first_frame(path: str | Path, max_size: int = 256) -> Image.Image | None:
    """Grab the first frame as a PIL image (for a thumbnail). None on failure."""
    proc = ffmpeg_tools.run(
        ["-i", str(path), "-frames:v", "1", "-f", "image2pipe",
         "-vcodec", "png", "-"], timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        img = Image.open(io.BytesIO(proc.stdout)).convert("RGB")
        img.thumbnail((max_size, max_size))
        return img
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Transcode to a DICOM-conformant H.264 elementary stream
# ---------------------------------------------------------------------------
# Count frames from ffmpeg's progress output (exact).
_FRAME_RE = re.compile(r"frame=\s*(\d+)")


def transcode_h264(path: str | Path, out_path: str | Path) -> int:
    """Transcode to H.264 High/4.1, yuv420p, even dims; return frame count."""
    out_path = Path(out_path)
    # High Profile / Level 4.1, 4:2:0, even dimensions (required by yuv420p).
    proc = ffmpeg_tools.run([
        "-i", str(path),
        "-an",                                  # no audio in the DICOM stream
        "-c:v", "libx264",
        "-profile:v", "high", "-level:v", "4.1",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-f", "h264",
        "-y", str(out_path),
    ], timeout=3600)
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        err = proc.stderr.decode("utf-8", "replace")[-800:]
        raise VideoError(f"ffmpeg transcode failed:\n{err}")
    frames = 0
    for m in _FRAME_RE.finditer(proc.stderr.decode("utf-8", "replace")):
        frames = int(m.group(1))
    return frames


# ---------------------------------------------------------------------------
# Build the DICOM Video Photographic Image Storage object
# ---------------------------------------------------------------------------
def build_video_dataset(stream: bytes, meta: VideoMeta, context: StudyContext,
                        series_info: SeriesInfo, *, series_uid: str,
                        instance_number: int = 1,
                        when: datetime.datetime | None = None) -> FileDataset:
    if not context.study_uid:
        raise ValueError("StudyContext.study_uid is required (reconcile first).")

    when = when or datetime.datetime.now()
    da = when.strftime("%Y%m%d")
    tm = when.strftime("%H%M%S")
    sop_uid = generate_uid()
    nframes = meta.nframes or 1
    fps = meta.fps or 25.0

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = VIDEO_SOP_CLASS_UID
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = H264_TRANSFER_SYNTAX
    file_meta.ImplementationClassUID = IMPLEMENTATION_CLASS_UID
    file_meta.ImplementationVersionName = IMPLEMENTATION_VERSION_NAME

    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\x00" * 128)
    ds.SpecificCharacterSet = "ISO_IR 100"

    # Patient + existing study
    ds.PatientName = context.patient_name or ""
    ds.PatientID = context.patient_id or ""
    ds.PatientBirthDate = context.birth_date or ""
    ds.PatientSex = context.sex or ""
    ds.StudyInstanceUID = context.study_uid
    ds.StudyDate = context.study_date or ""
    ds.StudyTime = context.study_time or ""
    ds.AccessionNumber = context.accession or ""
    ds.StudyID = context.study_id or ""
    ds.StudyDescription = context.study_description or ""
    ds.ReferringPhysicianName = context.referring_physician or ""

    # New series (video). XC = external-camera photography is the usual modality.
    ds.Modality = series_info.modality if series_info.modality not in ("", "OT") else "XC"
    ds.SeriesInstanceUID = series_uid
    ds.SeriesNumber = int(series_info.series_number)
    ds.SeriesDescription = series_info.series_description or ""
    if series_info.body_part:
        ds.BodyPartExamined = series_info.body_part
    ds.SeriesDate = da
    ds.SeriesTime = tm

    # SC equipment
    ds.ConversionType = "DV"            # Digitized Video
    ds.SecondaryCaptureDeviceManufacturer = "Print2PACS"

    # Image identification
    ds.InstanceNumber = int(instance_number)
    ds.ImageType = ["DERIVED", "SECONDARY"]
    ds.ContentDate = da
    ds.ContentTime = tm

    # Image pixel (H.264 -> YBR_PARTIAL_420)
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "YBR_PARTIAL_420"
    ds.PlanarConfiguration = 0
    ds.Rows = int(meta.height)
    ds.Columns = int(meta.width)
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0

    # Multi-frame + cine
    ds.NumberOfFrames = int(nframes)
    ds.FrameIncrementPointer = FrameTimeTag
    ds.FrameTime = f"{1000.0 / fps:.3f}"
    ds.CineRate = int(round(fps))
    ds.RecommendedDisplayFrameRate = int(round(fps))

    # Lossy compression markers (H.264)
    ds.LossyImageCompression = "01"
    ds.LossyImageCompressionMethod = "ISO_14496_10"

    # Encapsulated pixel data (single fragment = the elementary stream)
    ds.PixelData = encapsulate([stream])
    ds["PixelData"].is_undefined_length = True

    ds.SOPClassUID = VIDEO_SOP_CLASS_UID
    ds.SOPInstanceUID = sop_uid
    return ds


def convert_video(path: str | Path, context: StudyContext,
                  series_info: SeriesInfo, *,
                  series_uid: str | None = None) -> FileDataset:
    """Transcode a video file and wrap it as a Video Photographic DICOM object."""
    path = Path(path)
    meta = probe(path)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "stream.h264"
        frames = transcode_h264(path, out)
        if frames:
            meta.nframes = frames
        stream = out.read_bytes()
    series_uid = series_uid or generate_uid()
    log.info("Converted video %s -> %d frames, %dx%d @ %.2f fps",
             path.name, meta.nframes, meta.width, meta.height, meta.fps)
    return build_video_dataset(stream, meta, context, series_info,
                               series_uid=series_uid)
