# CLAUDE.md

Guidance for AI assistants (and new contributors) working in this repository.

## What this is

Print2PACS is a standalone, end-user Windows app: drop images / PDF / video / an
existing DICOM object, look up a study by **accession number** (C-FIND), convert
or re-file it to DICOM stamped with that study's patient/study metadata, and
C-STORE it as a **new series** on the existing study. The `engine/` + `net/` +
`model.py` layer is GUI-free and mirrors the DCMTools conventions so it can be
reused as a toolkit tab later.

## Layout

- `print2pacs/engine/` — `sc_import` (images/PDF → Secondary Capture),
  `video` (→ H.264 Video Photographic), `dicom_import` (re-file an existing
  object), `reconcile` (C-FIND lookup), `send` (C-STORE), `ffmpeg_tools`.
- `print2pacs/net/scu.py` — `c_echo` / `c_find` / `c_store`.
- `print2pacs/gui/` — `app.py` (3-step wizard), `settings_view`, `dialogs`,
  `widgets`, `theme`.
- `print2pacs/` — `model`, `config`, `paths`, `updater`, `logging_setup`.
- `tests/` — offline pytest against throwaway localhost SCPs.
- `scripts/` — `fetch_ffmpeg.py`, `make_icon.py`.

## Dev commands

    .venv\Scripts\python -m pytest             # full offline suite
    .venv\Scripts\python main.py               # run from source (no self-update)
    .venv\Scripts\python scripts\make_icon.py  # regenerate assets/icon.ico
    .venv\Scripts\pyinstaller Print2PACS.spec --noconfirm --clean

## Conventions & gotchas (learned the hard way)

- **8-bit uncompressed PixelData must be VR `OB`**, not `OW`. pydicom defaults to
  OW; strict archives reject it for `BitsAllocated=8`. See `sc_import`.
- Use **`SpecificCharacterSet` `ISO_IR 192`** (UTF-8); set `Manufacturer`
  (Type-2) and `ConversionType` (Type-1). Preserve color for palette/CMYK/RGBA
  sources; window >8-bit grayscale down to 8-bit.
- **Dropped DICOM objects** keep their native `Modality`/SOP Class; only patient +
  study tags are re-stamped, and new Series/SOP Instance UIDs are assigned. The
  128-byte preamble is zeroed and the object is rebuilt through pydicom
  (anti-polyglot, CVE-2019-11687). Private tags are kept unless the user opts to
  strip them.
- **Modality "Auto (by type)"** maps PDF/TIFF→`OT`, photos→`XC`, video→`XC`, and
  splits a mixed drop into one series per modality
  (`sc_import.suggest_modality` / `group_pages_by_modality`).
- Keep the `engine/` layer GUI-free so it stays reusable.

## Security & privacy (important)

- **Never commit real site data.** AE titles, hostnames, IP addresses, and PHI
  live only in the git-ignored `Print2PACS-data/` folder (plus any legacy or
  foreign config files, which are also git-ignored). The repo ships only
  placeholders.
- **Scan staged changes before every commit** for real AE titles, hostnames, IP
  ranges, site/organization names, and personal emails. The only contact that
  should appear in tracked files is the dedicated project address,
  `DICOMTool@gmail.com`.
- Self-update downloads release assets over HTTPS and **verifies SHA-256**; it
  never uploads anything, and only runs in the built `.exe`.

## Releasing

Releases are driven by the **commit subject on `main`** — no manual tags:

- `... [release]` → patch (vX.Y.**Z+1**)
- `... [release minor]` → minor
- `... [release major]` → major

CI (`.github/workflows/release.yml`) stamps the version into
`print2pacs/__init__.py`, runs the tests, builds the exe (bundling ffmpeg),
SHA-256s it, and publishes a GitHub Release. **A commit without the marker builds
nothing** — only add the marker to a commit you actually want shipped.
