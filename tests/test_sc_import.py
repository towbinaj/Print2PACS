"""Tests for the image/PDF -> Secondary Capture engine."""

from __future__ import annotations

from PIL import Image
from pydicom import dcmread

from print2pacs.engine import sc_import
from print2pacs.model import SeriesInfo, StudyContext

CTX = StudyContext(
    patient_id="P1", patient_name="DOE^JANE^A", birth_date="19700101",
    sex="F", study_uid="1.2.3.4.5", study_date="20240101",
    accession="ACC1", study_description="TEST STUDY", study_id="S1")
SI = SeriesInfo(modality="OT", series_description="Unit Test", body_part="CHEST")


def _page(mode, size, color):
    return sc_import.SourcePage(Image.new(mode, size, color), f"{mode}.png")


def test_supported_extensions():
    assert sc_import.is_supported("a.JPG")
    assert sc_import.is_supported("b.pdf")
    assert sc_import.is_supported("c.tiff")
    assert not sc_import.is_supported("d.txt")
    assert not sc_import.is_supported("e.docx")


def test_rgb_conversion():
    ds = sc_import.build_sc_dataset(
        Image.new("RGB", (200, 120), (10, 20, 30)), CTX, SI,
        series_uid="9.9.9", instance_number=1)
    assert ds.SOPClassUID == sc_import.SC_SOP_CLASS_UID
    assert ds.PhotometricInterpretation == "RGB"
    assert ds.SamplesPerPixel == 3
    assert ds.PlanarConfiguration == 0
    assert (ds.Rows, ds.Columns) == (120, 200)
    assert ds.BitsAllocated == 8 and ds.BitsStored == 8 and ds.HighBit == 7
    assert len(ds.PixelData) == 200 * 120 * 3
    assert ds.StudyInstanceUID == "1.2.3.4.5"      # existing study
    assert ds.SeriesInstanceUID == "9.9.9"
    assert ds.Modality == "OT"
    assert ds.ConversionType == "WSD"
    assert list(ds.ImageType) == ["DERIVED", "SECONDARY"]


def test_grayscale_conversion():
    ds = sc_import.build_sc_dataset(
        Image.new("L", (160, 90), 128), CTX, SI,
        series_uid="9.9.9", instance_number=1)
    assert ds.PhotometricInterpretation == "MONOCHROME2"
    assert ds.SamplesPerPixel == 1
    assert "PlanarConfiguration" not in ds
    assert len(ds.PixelData) == 160 * 90


def test_rgba_flattened_to_rgb():
    img = Image.new("RGBA", (40, 40), (255, 0, 0, 128))
    ds = sc_import.build_sc_dataset(img, CTX, SI, series_uid="9.9.9",
                                    instance_number=1)
    assert ds.PhotometricInterpretation == "RGB"
    assert len(ds.PixelData) == 40 * 40 * 3


def test_series_shares_uid_distinct_sop():
    pages = [_page("RGB", (10, 10), (1, 2, 3)),
             _page("L", (10, 10), 50),
             _page("RGB", (10, 10), (9, 9, 9))]
    out = sc_import.convert_pages(pages, CTX, SI)
    assert len(out) == 3
    series_uids = {d.SeriesInstanceUID for d in out}
    assert len(series_uids) == 1
    sop_uids = {d.SOPInstanceUID for d in out}
    assert len(sop_uids) == 3
    assert [d.InstanceNumber for d in out] == [1, 2, 3]
    assert all(d.StudyInstanceUID == "1.2.3.4.5" for d in out)


def test_requires_study_uid():
    bad = StudyContext()  # no study_uid
    try:
        sc_import.build_sc_dataset(Image.new("L", (4, 4), 0), bad, SI,
                                   series_uid="1", instance_number=1)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_palette_color_preserved():
    # A palette image with color must NOT be flattened to grayscale.
    import numpy as np
    a = np.zeros((20, 40, 3), np.uint8)
    a[:, :20] = (220, 20, 20)        # red half
    pal = Image.fromarray(a, "RGB").convert("P", palette=Image.ADAPTIVE)
    ds = sc_import.build_sc_dataset(pal, CTX, SI, series_uid="9",
                                    instance_number=1)
    assert ds.PhotometricInterpretation == "RGB"
    left = ds.pixel_array[:, :20]
    r, g, b = left[..., 0].mean(), left[..., 1].mean(), left[..., 2].mean()
    assert r > g + 40 and r > b + 40, "palette color was lost"


def test_cmyk_to_rgb():
    img = Image.new("RGB", (10, 10), (200, 10, 10)).convert("CMYK")
    ds = sc_import.build_sc_dataset(img, CTX, SI, series_uid="9",
                                    instance_number=1)
    assert ds.PhotometricInterpretation == "RGB"
    assert ds.SamplesPerPixel == 3


def test_16bit_windowed_to_8bit():
    import numpy as np
    ramp = np.tile(np.linspace(0, 65535, 50, dtype=np.uint16), (10, 1))
    img = Image.fromarray(ramp)           # mode I;16
    ds = sc_import.build_sc_dataset(img, CTX, SI, series_uid="9",
                                    instance_number=1)
    assert ds.PhotometricInterpretation == "MONOCHROME2"
    assert ds.BitsAllocated == 8
    arr = ds.pixel_array
    assert arr.min() == 0 and arr.max() == 255     # full window visible


def test_gif_not_supported():
    assert not sc_import.is_supported("x.gif")
    assert ".gif" not in sc_import.SUPPORTED_EXTS


def test_save_and_reread(tmp_path):
    out = sc_import.convert_pages([_page("RGB", (20, 10), (5, 5, 5))], CTX, SI)
    files = sc_import.save_datasets(out, tmp_path)
    assert len(files) == 1 and files[0].exists()
    re = dcmread(str(files[0]))
    assert re.SOPClassUID == sc_import.SC_SOP_CLASS_UID
    assert str(re.file_meta.TransferSyntaxUID) == "1.2.840.10008.1.2.1"
    assert re.pixel_array.shape == (10, 20, 3)


def test_rle_compression_lossless():
    from pydicom.uid import RLELossless
    img = Image.new("RGB", (64, 48), (200, 10, 10))
    ds = sc_import.build_sc_dataset(img, CTX, SI, series_uid="1",
                                    instance_number=1, compression="rle")
    assert ds.file_meta.TransferSyntaxUID == RLELossless
    arr = ds.pixel_array                      # needs RLE decode
    assert arr.shape == (48, 64, 3)
    assert tuple(int(x) for x in arr[0, 0]) == (200, 10, 10)   # lossless


def test_pixeldata_vr_is_ob_for_8bit():
    # 8-bit uncompressed pixel data must be OB (not OW) or strict archives reject.
    rgb = sc_import.build_sc_dataset(Image.new("RGB", (8, 8), (1, 2, 3)), CTX,
                                     SI, series_uid="1", instance_number=1)
    assert rgb["PixelData"].VR == "OB"
    gray = sc_import.build_sc_dataset(Image.new("L", (8, 8), 0), CTX, SI,
                                      series_uid="1", instance_number=1)
    assert gray["PixelData"].VR == "OB"


def test_charset_and_manufacturer():
    ds = sc_import.build_sc_dataset(Image.new("L", (4, 4), 0), CTX, SI,
                                    series_uid="1", instance_number=1)
    assert ds.SpecificCharacterSet == "ISO_IR 192"     # UTF-8
    assert ds.Manufacturer == "Print2PACS"             # Type-2 present


def test_unicode_emoji_name_roundtrips(tmp_path):
    ctx = StudyContext(patient_id="P", patient_name="TEST^\U0001F600é",
                       study_uid="1.2.3")
    out = sc_import.convert_pages([_page("L", (8, 8), 0)], ctx, SI)
    files = sc_import.save_datasets(out, tmp_path)
    re = dcmread(str(files[0]))
    assert str(re.PatientName) == "TEST^\U0001F600é"
    assert re.SpecificCharacterSet == "ISO_IR 192"


def test_suggest_modality_by_type():
    assert sc_import.suggest_modality("report.pdf") == "OT"
    assert sc_import.suggest_modality("scan.TIFF") == "OT"
    assert sc_import.suggest_modality("x.tif") == "OT"
    assert sc_import.suggest_modality("photo.jpg") == "XC"
    assert sc_import.suggest_modality("photo.JPEG") == "XC"
    assert sc_import.suggest_modality("shot.png") == "XC"
    assert sc_import.suggest_modality("pic.bmp") == "XC"
    assert sc_import.suggest_modality("mystery.xyz") == "OT"   # fallback


def test_auto_split_groups_and_applies_modality():
    from dataclasses import replace
    pages = [_page("RGB", (8, 8), (1, 1, 1)),           # RGB.png -> XC
             sc_import.SourcePage(Image.new("RGB", (8, 8), (2, 2, 2)), "doc.pdf"),
             sc_import.SourcePage(Image.new("RGB", (8, 8), (3, 3, 3)), "a.jpg")]
    groups = sc_import.group_pages_by_modality(pages)
    assert set(groups) == {"XC", "OT"}
    assert len(groups["XC"]) == 2 and len(groups["OT"]) == 1

    ot = sc_import.convert_pages(groups["OT"], CTX, replace(SI, modality="OT"))
    xc = sc_import.convert_pages(groups["XC"], CTX, replace(SI, modality="XC"))
    assert ot[0].Modality == "OT"
    assert xc[0].Modality == "XC"
    assert ot[0].SeriesInstanceUID != xc[0].SeriesInstanceUID   # separate series


def test_multiframe_tiff(tmp_path):
    p = tmp_path / "multi.tiff"
    frames = [Image.new("L", (12, 8), v) for v in (10, 20, 30)]
    frames[0].save(p, save_all=True, append_images=frames[1:])
    pages = sc_import.load_pages(p)
    assert len(pages) == 3
    assert all(pg.source == "multi.tiff" for pg in pages)
    assert [pg.page_index for pg in pages] == [0, 1, 2]


def test_sc_conformance_attributes_present():
    # R14: required/conditional attributes flagged by IOD validation are present
    # (empty, since a converted document/photo has no laterality/orientation).
    ds = sc_import.convert_pages([_page("L", (8, 8), 5)], CTX, SI)[0]
    assert "Laterality" in ds and ds.Laterality == ""
    assert "PatientOrientation" in ds
