"""DICOM SCU operations: C-ECHO, C-FIND, C-STORE.

A focused subset with the same shape as DCMTools' ``net/scu.py`` (same function
names, ``Node``-based signatures, ``progress`` callback), so engine code written
against this ports to the toolkit unchanged.

Each call is synchronous and meant to run on a worker thread from the UI.
"""

from __future__ import annotations

import ssl
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
# TLS
# ---------------------------------------------------------------------------
def tls_args_for(node: Node):
    """Return pynetdicom ``tls_args`` for a node, or ``None`` for cleartext.

    When the node has ``tls=True`` this builds a client ``SSLContext`` that
    **verifies the peer certificate and its hostname** against the system trust
    store, and returns ``(ctx, node.host)`` for ``AE.associate(tls_args=...)``.

    There is deliberately no cleartext fallback: if a destination is marked for
    TLS, the association is attempted over TLS only. A failed handshake raises
    rather than silently downgrading to plaintext. (A private-CA / client-cert
    provisioning layer is a separate, hospital-managed concern.)
    """
    if not getattr(node, "tls", False):
        return None
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return (ctx, node.host)


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
    truncated: bool = False      # stopped early at max_results (too many matches)


@dataclass
class StoreResult:
    sent: int = 0
    failed: int = 0
    warnings: int = 0
    uncertain: int = 0
    errors: list[str] = field(default_factory=list)
    warning_details: list[str] = field(default_factory=list)
    uncertain_details: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.sent + self.failed

    @property
    def clean(self) -> bool:
        """True only when every object was accepted with no warning/failure."""
        return (self.sent > 0 and self.failed == 0 and self.warnings == 0
                and self.uncertain == 0)


def classify_store_status(status) -> tuple[str, str]:
    """Map a C-STORE response status to (state, code).

    States: ``sent`` (success), ``warning`` (accepted with coercion/discard),
    ``rejected`` (the archive refused it), ``uncertain`` (no usable response -
    the object may or may not have been stored).
    """
    if status and status.Status == 0x0000:
        return "sent", "0x0000"
    if status and status.Status in (0xB000, 0xB006, 0xB007):
        return "warning", f"0x{status.Status:04x}"
    if status:
        return "rejected", f"0x{status.Status:04x}"
    return "uncertain", "no-response"


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
           progress: Progress = _noop, tls_args=None,
           max_results: int | None = None) -> FindResult:
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
                if max_results and len(result.datasets) >= max_results:
                    result.truncated = True     # too many; stop reading
                    break
            elif status and status.Status == 0x0000:
                result.success = True
            elif status:
                # A non-pending, non-success status is a failure/abort. Record
                # it so the caller can refuse to trust a partial result set.
                result.message = f"C-FIND failed with status 0x{status.Status:04x}."
                progress(result.message)
        if result.truncated:
            result.message = f"More than {max_results} matches."
        elif not result.message:
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
            timeout: int | None = None, tls_args=None,
            on_result: Callable[[Path, str, str], None] | None = None
            ) -> StoreResult:
    """Send DICOM files via C-STORE over one association.

    ``on_result(file, state, code)`` is called for every object that is actually
    attempted, with ``state`` from :func:`classify_store_status`. Objects not
    attempted (association lost / cancelled) are left for the caller to resume -
    they are not reported as failed.
    """
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
                # Connection dropped between objects: the rest were NOT attempted,
                # so leave them for the caller to resume (do not mark failed).
                result.errors.append(
                    f"Association lost during transfer; {total - i + 1} object(s)"
                    " not attempted.")
                break
            try:
                ds = dcmread(str(f), force=True)
                status = assoc.send_c_store(ds)
                state, code = classify_store_status(status)
            except Exception as exc:  # noqa: BLE001
                # Bytes may have reached the archive before the error -> uncertain.
                state, code = "uncertain", f"error: {str(exc)[:160]}"

            if state == "sent":
                result.sent += 1
            elif state == "warning":
                # Accepted, but coerced/discarded: counted as sent yet recorded
                # separately so it never reads as a clean success.
                result.sent += 1
                result.warnings += 1
                result.warning_details.append(
                    f"{f.name}: accepted with warning {code}")
            elif state == "uncertain":
                result.uncertain += 1
                result.uncertain_details.append(f"{f.name}: {code}")
            else:  # rejected
                result.failed += 1
                result.errors.append(f"{f.name}: {code}")

            if on_result is not None:
                on_result(f, state, code)
            if on_frac:
                on_frac(i / total)
    finally:
        assoc.release()

    progress(f"Done. Sent {result.sent}, failed {result.failed}.")
    return result
