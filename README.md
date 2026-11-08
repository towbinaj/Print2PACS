# Print2PACS

Send printed, scanned, or exported images to a DICOM PACS as a new series on an
**existing study** — a modern, free and open-source rebuild of the old
*DCMPrint2PACS* tool, without the virtual printer driver.

Instead of printing to a virtual driver, you **drag images, a PDF, a video, or an
existing DICOM object into the app**, look up the study by **accession number**,
and Print2PACS converts them to DICOM (**Secondary Capture** for images, **Video
Photographic Image Storage / H.264** for video) — or, for a dropped DICOM object,
**re-files it as-is** — stamps everything with the study's patient/study metadata,
and stores it to PACS as a new series.

> **Audience:** this is the *standalone, end-user* app (simple 3-step wizard).
> The same engine is designed to drop into the
> [DICOM Toolkit (DCMTools)](https://github.com/towbinaj/DCMTools) later as a tab
> for IT staff.

## How it works

1. **Select items** — drag in or browse for images `JPG · PNG · TIFF · BMP · PDF`,
   video `MP4 · MOV · AVI · MKV`, or existing **DICOM objects** `.dcm` (detected by
   content, so any extension works). Multi-page TIFF/PDF expand to one image per
   page; each video becomes its own series; dropped DICOM objects re-file as one
   new series keeping their native modality. Reorder or remove items.
   (Image color is preserved, incl. palette/CMYK/transparency; grayscale stays
   grayscale; 16-bit grayscale is preserved as 16-bit with a display window;
   camera photos are auto-rotated via their EXIF orientation.)
2. **Find the study** — pick your PACS, enter the **Accession #** (or Patient ID),
   and look it up (C-FIND). Confirm the returned patient/study.
3. **Review and send** — set the new series details (modality, description, body
   part, capture type) and **Send to PACS** (C-STORE). The images become a new
   Secondary Capture series under the original accession. **Modality defaults to
   "Auto (by type)"**: PDF/TIFF → `OT`, photos (JPG/PNG/BMP) → `XC`, video →
   `XC`; a dropped DICOM object keeps its own. A mixed drop files as one series
   per modality. Pick a specific code to force one instead.

You can **drop files anywhere in the window**, not just on the Step 1 banner.

## Running from source

```
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

Tests:

```
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

The suite runs fully offline against throwaway localhost SCPs (no real PACS).

## Settings & destinations

Click **⚙ Settings** (top-right) to manage everything, saved immediately and
remembered across runs:

- **Application** — My AE Title (the calling AE this app presents), appearance
  (System/Light/Dark), PDF import DPI, series number.
- **Destinations** — add / edit / delete PACS nodes (name, remote AE, host,
  port, calling AE, TLS).
- **Default Query PACS** and **Default Send PACS** — query one system and send
  to another if you want (e.g. query the PACS, send to a VNA); they default to
  the same. Step 2 picks the query target, Step 3 the send target; both are
  remembered. Use **Test** in Step 2 to C-ECHO the query node.

On first run there are no PACS configured — add one in Settings (or the quick
**＋ Add PACS…** in Step 2).

Settings and destinations are stored in a portable **`Print2PACS-data\`** folder
next to the executable (falling back to `%APPDATA%\Print2PACS`). A placeholder
template is in [`data/destinations.sample.json`](data/destinations.sample.json).

## Video support

DICOM does not store AVI/MP4 containers — it carries an encapsulated **H.264**
(or MPEG-2 / HEVC) stream. Print2PACS transcodes any common video to a
conformant **MPEG-4 AVC/H.264 High Profile @ Level 4.1** stream (`yuv420p`,
`YBR_PARTIAL_420`) and wraps it as a **Video Photographic Image Storage** object
(transfer syntax `1.2.840.10008.1.2.4.102`) attached to the chosen study as a
new series.

This uses **ffmpeg**, bundled into the built `.exe`. For source runs, fetch it
once:

```
.venv\Scripts\python scripts\fetch_ffmpeg.py
```

This downloads a static ffmpeg into `assets\ffmpeg\` (git-ignored, ~160 MB).
If ffmpeg is already on your PATH, that is used instead. The bundled binary is a
GPL build of ffmpeg; distribute accordingly.

> 🔒 **`Print2PACS-data\` is git-ignored and never committed.** It holds your
> site's real AE titles, hosts, and ports. The repo ships only placeholders
> (RFC 5737 doc IPs, `TESTPACS`).

## Importing existing DICOM objects

Drop a `.dcm` (or any DICOM file — it's detected by content, not extension) and
Print2PACS **re-files it onto the chosen study** instead of rasterizing it: the
object keeps its native pixels, **Modality** and SOP Class, while its
patient/study tags are re-stamped to match the looked-up study and it is given a
fresh Series + SOP Instance UID so it lands as a new series without colliding
with the original. Several dropped objects become one new series.

**Safe handling (no embedded entrypoint).** Non-DICOM inputs are already safe —
images/PDF are rasterized to pixels and video is re-encoded, so nothing in the
original container survives. For DICOM objects the extra care is:

- Every object is **parsed and rebuilt by pydicom** — the bytes sent to PACS are
  regenerated, never the dropped file verbatim.
- The unused 128-byte **file preamble is zeroed**, which neutralizes the known
  *DICOM-as-PE* polyglot ([CVE-2019-11687](https://nvd.nist.gov/vuln/detail/CVE-2019-11687)):
  a file crafted to be both a valid `.dcm` and a runnable Windows `.exe`. The
  re-filed object cannot double as a launchable binary.
- The object is validated as a real DICOM instance (SOP Class + transfer syntax)
  before anything is sent.
- **Private/vendor tags are kept by default**; turn on *Settings → Imported
  DICOM → Strip private/vendor tags* to drop opaque vendor blobs before sending.

## Building a single .exe

```
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python scripts\fetch_ffmpeg.py        # bundles video support
.venv\Scripts\pyinstaller Print2PACS.spec --noconfirm --clean
```

Produces `dist\Print2PACS.exe` — one portable file (large, ~200 MB, because
ffmpeg is bundled for video). Copy it to a local, writable folder before running
(it creates `Print2PACS-data\` beside itself). The exe is unsigned, so Windows
SmartScreen may warn on first launch (*More info → Run anyway*). The build still
works without running `fetch_ffmpeg.py`; video then needs ffmpeg on the PATH.

## Updates (self-update)

Like the DICOM Toolkit, the built exe can update itself from GitHub Releases:

- On launch it quietly checks the latest release; if a newer version exists, an
  **⬆ Update to vX.Y.Z** button appears in the status bar. There's also a
  **Check for updates** button in **⚙ Settings**.
- Clicking it downloads the new exe (**SHA-256 verified**), swaps it in, and
  reopens on the new version. Your `Print2PACS-data\` (settings, destinations,
  logs) is left untouched — it lives beside the exe, not inside it.
- Only HTTPS **GET** requests to GitHub are made; nothing is ever uploaded.
  Offline/firewalled machines simply skip the check. Self-update applies to the
  built exe only; from source, update with `git`.

### Releasing a new version (maintainer)

Releases are driven by the **commit subject** on `main` — no buttons, no manual
tags. CI ([`.github/workflows/release.yml`](.github/workflows/release.yml))
bumps the version, builds the exe on a Windows runner (fetches ffmpeg, runs the
tests, PyInstaller), SHA-256s it, and publishes a Release that installed apps
then offer via self-update:

```
git commit -m "fix body part field [release]"       # patch -> vX.Y.(Z+1)
git commit -m "add video support [release minor]"    # minor -> vX.(Y+1).0
git commit -m "breaking config change [release major]"  # major -> v(X+1).0.0
git push
```

A commit with no `[release]` marker never releases. A manually pushed `vX.Y.Z`
tag also triggers a build (fallback).

## Project layout

```
print2pacs/
  paths.py, logging_setup.py, model.py, config.py, updater.py
  net/      scu.py         (c_echo / c_find / c_store - DCMTools-compatible)
  engine/   sc_import.py   (images/PDF -> Secondary Capture DICOM)  <- new core
            video.py       (video -> H.264 Video Photographic DICOM)
            dicom_import.py(re-file an existing DICOM object onto the study)
            ffmpeg_tools.py(locate/run bundled or system ffmpeg)
            reconcile.py   (C-FIND study lookup -> StudyContext)
            send.py        (write + C-STORE a converted series)
  gui/      app.py (3-step wizard) + settings_view.py + dialogs.py
            + widgets.py + theme.py
scripts/  fetch_ffmpeg.py  (download ffmpeg for bundling)
main.py                    entry point
tests/                     offline pytest suite (localhost SCPs)
```

The `engine/` + `net/` + `model.py` modules are GUI-free and mirror the DCMTools
conventions, so the image→DICOM capability can be added to the toolkit as a tab
with minimal rework.

## Notes & limitations

- Image output is 8-bit Secondary Capture (`MONOCHROME2` or `RGB`), uncompressed
  (Explicit VR Little Endian) — maximally compatible with PACS/VNAs.
- Video output is encapsulated H.264 (High/4.1). **Audio is dropped.** The
  receiving PACS/VNA must negotiate the H.264 presentation context
  (`1.2.840.10008.1.2.4.102`) — some archives reject video; verify with yours.
- Only formats that map to a valid DICOM object are accepted; GIF and other
  unsupported types are ignored.
- A dropped DICOM object is re-filed with its **original Modality / SOP Class**
  preserved (the Step 3 modality menu applies to converted images/video only).
- Burned-in PHI in the source is **not** removed; review items before sending.
- Final validation against a production PACS must be done on your own network.

## License

Print2PACS is released under the **GNU General Public License v3.0** — see
[`LICENSE`](LICENSE). You may use, study, modify and redistribute it under the
GPL-3.0 terms.

This choice reflects the tool's dependencies: it bundles **FFmpeg** (GPL build,
for video) and uses **PyMuPDF** (AGPL/commercial dual-licensed, for PDF import).
If you intend to redistribute Print2PACS — especially in or beyond an
organization — review FFmpeg's and PyMuPDF's terms (a PyMuPDF commercial license
may be required for some uses) and keep the corresponding source/notices. This
note is not legal advice.
