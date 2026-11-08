"""Print2PACS - send printed/scanned images to a DICOM PACS as a new series.

A standalone, end-user-facing tool: drop images (or a PDF), look up the existing
study by accession number, and store the pages to PACS as a Secondary Capture
series on that study.

The engine (``print2pacs.engine``) is intentionally GUI-free and mirrors the
DICOM Toolkit (DCMTools) conventions so it can later be dropped into that app as
a tab for IT staff.
"""

from __future__ import annotations

APP_NAME = "Print2PACS"
__version__ = "1.0.7"
