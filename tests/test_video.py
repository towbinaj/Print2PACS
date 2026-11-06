"""Tests for the video -> DICOM engine.

The DICOM object construction, encapsulation and C-STORE are tested with a
synthetic byte stream (no ffmpeg). Probe parsing is tested by feeding canned
ffmpeg output. Actual transcoding needs ffmpeg and is covered by an integration
test that skips when ffmpeg is unavailable.
"""

from __future__ import annotations

import subprocess
import types

from pydicom import dcmread

from print2pacs.engine import ffmpeg_tools, send, video
from print2pacs.engine import reconcile
from print2pacs.model import SeriesInfo, StudyContext

CTX = StudyContext(patient_id="P1", patient_name="DOE^JANE", birth_date="19700101",
                   sex="F", study_uid="1.2.3.4.5", accession="ACC1",
                   study_description="TEST")
SI = SeriesInfo(series_description="Video Test", body_part="HEART")
META = video.VideoMeta(width=640, height=480, fps=30.0, nframes=90, duration=3.0)
FAKE_STREAM = b"\x00\x00\x00\x01" + b"fakeh264" * 50   # pretend elementary stream


def test_is_video():
    assert video.is_video("clip.mp4")
    assert video.is_video("a.AVI")
    assert video.is_video("b.mov")
    assert not video.is_video("c.jpg")
    assert not video.is_video("d.pdf")


def test_build_video_dataset_attributes():
    ds = video.build_video_dataset(FAKE_STREAM, META, CTX, SI, series_uid="9.9")
    assert ds.SOPClassUID == video.VIDEO_SOP_CLASS_UID
    assert ds.file_meta.TransferSyntaxUID == video.H264_TRANSFER_SYNTAX
    assert ds.PhotometricInterpretation == "YBR_PARTIAL_420"
    assert ds.SamplesPerPixel == 3
    assert (ds.Rows, ds.Columns) == (480, 640)
    assert ds.NumberOfFrames == 90
    assert ds.CineRate == 30
    assert abs(float(ds.FrameTime) - 1000.0 / 30.0) < 0.01
    assert ds.StudyInstanceUID == "1.2.3.4.5"       # existing study
    assert ds.SeriesInstanceUID == "9.9"
    assert ds.Modality == "XC"
    assert ds.ConversionType == "DV"
    assert ds.LossyImageCompressionMethod == "ISO_14496_10"
    # encapsulated, undefined length
    assert ds["PixelData"].is_undefined_length


def test_video_dataset_roundtrip_and_store(pacs, tmp_path):
    ds = video.build_video_dataset(FAKE_STREAM, META, CTX, SI, series_uid="9.9")
    from print2pacs.engine import sc_import
    files = sc_import.save_datasets([ds], tmp_path)
    re = dcmread(str(files[0]))
    assert re.SOPClassUID == video.VIDEO_SOP_CLASS_UID
    assert re.NumberOfFrames == 90
    # C-STORE to the fake PACS (which now supports the H.264 context)
    result = send.send_series("ME", pacs.node, [ds], tmp_path / "vjob")
    assert result.sent == 1 and result.failed == 0
    assert len(pacs.received) == 1
    assert pacs.received[0].SOPClassUID == video.VIDEO_SOP_CLASS_UID


def test_probe_parses_ffmpeg_output(monkeypatch):
    sample = (
        b"Input #0, mov,mp4,m4a, from 'clip.mp4':\n"
        b"  Duration: 00:00:03.50, start: 0.000000, bitrate: 1200 kb/s\n"
        b"  Stream #0:0(und): Video: h264 (High), yuv420p, 1280x720, "
        b"1180 kb/s, 29.97 fps, 30 tbr, 90k tbn\n")

    def fake_run(args, **kw):
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=sample)

    monkeypatch.setattr(ffmpeg_tools, "run", fake_run)
    meta = video.probe("clip.mp4")
    assert (meta.width, meta.height) == (1280, 720)
    assert abs(meta.fps - 29.97) < 0.01
    assert abs(meta.duration - 3.5) < 0.01
    assert meta.nframes == round(3.5 * 29.97)


def test_full_video_pipeline_integration(pacs, tmp_path):
    """End-to-end: synth video -> transcode -> DICOM -> store. Needs ffmpeg."""
    import pytest
    if not ffmpeg_tools.have_ffmpeg():
        pytest.skip("ffmpeg not available")
    src = tmp_path / "src.mp4"
    try:
        # Generate a 2s test pattern with the bundled/system ffmpeg.
        proc = ffmpeg_tools.run(
            ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=2",
             "-pix_fmt", "yuv420p", "-y", str(src)], timeout=120)
        if proc.returncode != 0 or not src.exists():
            pytest.skip("ffmpeg could not generate a test clip")
        ctx = reconcile.lookup_studies("ME", pacs.node, accession="ACC1")[0]
        ds = video.convert_video(src, ctx, SI)
    except (OSError, PermissionError, subprocess.SubprocessError,
            video.VideoError, ffmpeg_tools.FFmpegNotFound) as exc:
        pytest.skip(f"ffmpeg could not be executed here: {exc}")
    assert ds.SOPClassUID == video.VIDEO_SOP_CLASS_UID
    assert ds.NumberOfFrames > 0
    result = send.send_series("ME", pacs.node, [ds], tmp_path / "vjob2")
    assert result.sent == 1 and result.failed == 0
