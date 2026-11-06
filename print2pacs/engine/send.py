"""Persist converted datasets and store them to PACS (C-STORE)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from pydicom.dataset import FileDataset

from ..logging_setup import get_logger
from ..model import Node
from ..net import scu
from . import sc_import

log = get_logger("send")

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def send_series(my_aetitle: str, node: Node, datasets: list[FileDataset],
                out_dir: str | Path, *, progress: Progress = _noop,
                on_frac: Callable[[float], None] | None = None,
                should_cancel: Callable[[], bool] | None = None,
                cleanup: bool = True, tls_args=None) -> scu.StoreResult:
    """Write ``datasets`` under ``out_dir`` and C-STORE them to ``node``.

    ``out_dir`` is a per-job temp folder; it is removed afterward when
    ``cleanup`` is True (regardless of send success).
    """
    out_dir = Path(out_dir)
    progress(f"Writing {len(datasets)} DICOM file(s)...")
    files = sc_import.save_datasets(datasets, out_dir)
    try:
        result = scu.c_store(my_aetitle, node, files, progress=progress,
                             on_frac=on_frac, should_cancel=should_cancel,
                             tls_args=tls_args)
    finally:
        if cleanup:
            shutil.rmtree(out_dir, ignore_errors=True)
    return result
