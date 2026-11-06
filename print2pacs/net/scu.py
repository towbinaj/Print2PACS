"""DICOM SCU operations: C-ECHO, C-FIND, C-STORE.

A focused subset with the same shape as DCMTools' ``net/scu.py`` (same function
names, ``Node``-based signatures, ``progress`` callback), so engine code written
against this ports to the toolkit unchanged.

Each call is synchronous and meant to run on a worker thread from the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from pydicom import dcmread
from pydicom.dataset import Dataset
from pynetdicom import AE
from pynetdicom.presentation import (
    StoragePresentationContexts,
    VerificationPresentationContexts,
    build_context,
)
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind
from pynetdicom.sop_class import PatientRootQueryRetrieveInformationModelFind

from ..logging_setup import get_logger
from ..model import Node

log = get_logger("scu")

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class EchoResult:
    success: bool
    message: str


@dataclass
class FindResult:
    datasets: list[Dataset] = field(default_factory=list)
    message: str = ""
    success: bool = False


@dataclass
class StoreResult:
    sent: int = 0
    failed: int = 0
    warnings: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.sent + self.failed


# ---------------------------------------------------------------------------
# C-ECHO (connection test)
# ---------------------------------------------------------------------------
def c_echo(my_aetitle: str, node: Node, timeout: int | None = None,
           progress: Progress = _noop, tls_args=None) -> EchoResult:
    timeout = timeout or node.timeout
    ae = AE(ae_title=node.calling(my_aetitle))
    ae.requested_contexts = VerificationPresentationContexts
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = timeout

    progress(f"Associating with {node.aetitle}@{node.host}:{node.port} ...")
    assoc = ae.associate(node.host, node.port, ae_title=node.aetitle,
                         tls_args=tls_args)
    if not assoc.is_established:
        return EchoResult(False, "Association rejected / aborted / failed.")
    try:
        status = assoc.send_c_echo()
        if status and status.Status == 0x0000:
            return EchoResult(True, "Success - remote AE responded to C-ECHO.")
        code = f"0x{status.Status:04x}" if status else "no response"
        return EchoResult(False, f"C-ECHO returned status {code}.")
    finally:
        assoc.release()


# ---------------------------------------------------------------------------
# C-FIND
# ---------------------------------------------------------------------------
_FIND_MODELS = {
    "PATIENT": PatientRootQueryRetrieveInformationModelFind,
    "STUDY": StudyRootQueryRetrieveInformationModelFind,
}


def build_query(level: str, **criteria: str) -> Dataset:
    """Build a C-FIND identifier. Empty-string values are 'return this attr'."""
    ds = Dataset()
    ds.QueryRetrieveLevel = level
    for key, value in criteria.items():
        setattr(ds, key, value)
    return ds


def c_find(my_aetitle: str, node: Node, identifier: Dataset,
           model: str = "STUDY", timeout: int | None = None,
           progress: Progress = _noop, tls_args=None) -> FindResult:
    timeout = timeout or max(node.timeout, 60)
    result = FindResult()
    find_model = _FIND_MODELS[model.upper()]

    ae = AE(ae_title=node.calling(my_aetitle))
    ae.add_requested_context(find_model)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = timeout

    progress(f"Associating with {node.aetitle}@{node.host}:{node.port} ...")
    assoc = ae.associate(node.host, node.port, ae_title=node.aetitle,
                         tls_args=tls_args)
    if not assoc.is_established:
        result.message = "Association rejected / aborted / failed."
        return result
    try:
        for status, ds in assoc.send_c_find(identifier, find_model):
            if status and status.Status in (0xFF00, 0xFF01) and ds:
                result.datasets.append(ds)
            elif status and status.Status == 0x0000:
                result.success = True
            elif status:
                progress(f"C-FIND status 0x{status.Status:04x}")
        result.message = f"{len(result.datasets)} match(es)."
    finally:
        assoc.release()
    return result


# ---------------------------------------------------------------------------
# C-STORE
# ---------------------------------------------------------------------------
def _contexts_for_files(files: list[Path]) -> list:
    """Propose exactly the SOP class(es) + transfer syntax(es) in the files."""
    from pydicom.uid import ExplicitVRLittleEndian
    contexts: dict[tuple[str, str], object] = {}
    for f in files:
        try:
            ds = dcmread(str(f), stop_before_pixels=True, force=True)
        except Exception:  # noqa: BLE001
            continue
        fm = getattr(ds, "file_meta", None)
        sop = getattr(fm, "MediaStorageSOPClassUID", None) if fm else None
        ts = getattr(fm, "TransferSyntaxUID", None) if fm else None
        sop = sop or getattr(ds, "SOPClassUID", None)
        ts = ts or ExplicitVRLittleEndian
        if not sop:
            continue
        key = (str(sop), str(ts))
        if key not in contexts and len(contexts) < 128:
            contexts[key] = build_context(sop, ts)
    ctxs = list(contexts.values())
    covered = {str(k[0]) for k in contexts}
    for cx in StoragePresentationContexts:
        if len(ctxs) >= 128:
            break
        if str(cx.abstract_syntax) not in covered:
            ctxs.append(cx)
            covered.add(str(cx.abstract_syntax))
    return ctxs


def c_store(my_aetitle: str, node: Node, files: Iterable[Path],
            progress: Progress = _noop,
            on_frac: Callable[[float], None] | None = None,
            should_cancel: Callable[[], bool] | None = None,
            timeout: int | None = None, tls_args=None) -> StoreResult:
    """Send DICOM files via C-STORE over one association."""
    timeout = timeout or node.timeout
    files = [Path(f) for f in files]
    result = StoreResult()
    total = len(files)
    if not files:
        result.errors.append("No files to send.")
        return result

    progress(f"Preparing to send {total} file(s)...")
    contexts = _contexts_for_files(files)
    if not contexts:
        result.errors.append("No readable DICOM presentation contexts.")
        result.failed = total
        return result

    ae = AE(ae_title=node.calling(my_aetitle))
    ae.requested_contexts = contexts
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = timeout

    progress(f"Associating with {node.aetitle}@{node.host}:{node.port} ...")
    assoc = ae.associate(node.host, node.port, ae_title=node.aetitle,
                         tls_args=tls_args)
    if not assoc.is_established:
        result.errors.append("Association rejected / aborted / failed.")
        result.failed = total
        return result

    try:
        for i, f in enumerate(files, 1):
            if should_cancel is not None and should_cancel():
                progress("Cancelled.")
                break
            if not assoc.is_established:
                result.errors.append("Association lost during transfer.")
                result.failed += (total - i + 1)
                break
            try:
                ds = dcmread(str(f), force=True)
                status = assoc.send_c_store(ds)
                if status and status.Status == 0x0000:
                    result.sent += 1
                elif status and status.Status in (0xB000, 0xB006, 0xB007):
                    result.sent += 1
                    result.warnings += 1
                else:
                    code = f"0x{status.Status:04x}" if status else "no-response"
                    result.failed += 1
                    result.errors.append(f"{f.name}: {code}")
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                result.errors.append(f"{f.name}: {str(exc)[:200]}")
            if on_frac:
                on_frac(i / total)
    finally:
        assoc.release()

    progress(f"Done. Sent {result.sent}, failed {result.failed}.")
    return result
