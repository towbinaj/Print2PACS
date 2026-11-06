"""Print2PACS engine: GUI-free DICOM logic.

- ``sc_import``  : raster images / PDF  ->  Secondary Capture DICOM datasets
- ``reconcile``  : look up an existing study on PACS (C-FIND) -> StudyContext
- ``send``       : write datasets and store them to PACS (C-STORE)

These modules depend only on pydicom / pynetdicom / Pillow (+ PyMuPDF for PDF)
and the local ``model`` / ``net`` modules, so they can be lifted into DCMTools.
"""
