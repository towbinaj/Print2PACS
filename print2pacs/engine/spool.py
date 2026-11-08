"""Durable job spool + journal for C-STORE sends (crash/partial-failure safe).

Each send is a *job*: its DICOM objects are serialized to a private per-job folder
with a ``journal.json`` that records, per object, its SOP Instance UID, content
hash and state. The journal is written BEFORE any network activity and updated as
each object is accepted/rejected, so that:

  * a partial failure can be retried by re-sending the SAME objects (same UIDs),
    which the archive de-duplicates -- no duplicate clinical content;
  * an interrupted run (crash / close) leaves a recoverable record instead of
    orphaned, unexplained DICOM files;
  * objects whose acceptance is *uncertain* are held for manual verification and
    never silently re-sent.

The folder holds PHI (the serialized objects); it relies on the data folder's OS
permissions and is removed once every object is confirmed stored.
"""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import paths
from ..logging_setup import get_logger
from ..model import Node, StudyContext
from ..net import scu
from . import sc_import

log = get_logger("spool")

JOURNAL = "journal.json"

# Per-object states.
PREPARED = "prepared"     # serialized, not yet confirmed stored
SENT = "sent"             # archive returned success
WARNING = "warning"       # accepted with coercion/discard (stored)
REJECTED = "rejected"     # archive refused it
UNCERTAIN = "uncertain"   # sent but acceptance unknown (connection dropped)

_DONE = (SENT, WARNING)              # successfully stored
_RESUMABLE = (PREPARED, REJECTED)    # safe to (re)send automatically
# UNCERTAIN is deliberately NOT resumable: it is held for manual verification.


def _noop(_: str) -> None:
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write(job_dir: Path, journal: dict) -> None:
    (job_dir / JOURNAL).write_text(json.dumps(journal, indent=2),
                                   encoding="utf-8")


def read_journal(job_dir: Path) -> dict:
    return json.loads((Path(job_dir) / JOURNAL).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# State queries
# ---------------------------------------------------------------------------
def counts(journal: dict) -> dict:
    c = {PREPARED: 0, SENT: 0, WARNING: 0, REJECTED: 0, UNCERTAIN: 0}
    for it in journal.get("items", []):
        c[it["state"]] = c.get(it["state"], 0) + 1
    return c


def is_resolved(journal: dict) -> bool:
    """True when every object has been successfully stored."""
    items = journal.get("items", [])
    return bool(items) and all(it["state"] in _DONE for it in items)


def node_from_journal(journal: dict) -> Node:
    d = journal.get("destination", {})
    return Node(name=d.get("name", ""), aetitle=d.get("aetitle", ""),
                host=d.get("host", ""), port=int(d.get("port", 104) or 104),
                tls=bool(d.get("tls", False)))


def summary(journal: dict) -> str:
    c = counts(journal)
    dest = journal.get("destination", {}).get("name", "?")
    return (f"{journal.get('patient', '?')}  ·  Acc "
            f"{journal.get('accession', '?')}  →  {dest}\n"
            f"{c[SENT] + c[WARNING]} stored · {c[PREPARED] + c[REJECTED]} unsent "
            f"· {c[UNCERTAIN]} uncertain  ·  started "
            f"{journal.get('created', '?')}")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
class DiskFull(Exception):
    """Raised when staging a job runs out of disk space."""


def prepare_job(datasets, node: Node, context: StudyContext) -> Path:
    """Serialize ``datasets`` to a new job folder and write the journal.

    On a disk-full error nothing partial is left behind and ``DiskFull`` is
    raised with a clear message.
    """
    job_dir = paths.new_job_dir()
    try:
        files = sc_import.save_datasets(datasets, job_dir)
        items = []
        for ds, f in zip(datasets, files):
            items.append({
                "file": f.name,
                "sop_instance_uid": str(getattr(ds, "SOPInstanceUID", "")),
                "sha256": _sha256(f),
                "state": PREPARED,
                "code": "",
            })
        journal = {
            "created": _now(),
            "destination": {"name": node.name, "aetitle": node.aetitle,
                            "host": node.host, "port": int(node.port),
                            "tls": bool(node.tls)},
            "study_uid": context.study_uid,
            "accession": context.accession,
            "patient": str(context.patient_name or ""),
            "items": items,
        }
        _write(job_dir, journal)
    except OSError as exc:
        discard_job(job_dir)                 # don't leave a half-written job
        if exc.errno == errno.ENOSPC:
            raise DiskFull("Not enough disk space to prepare this send. "
                           "Free up space and try again.") from exc
        raise
    log.info("Spooled job %s: %d object(s) for study %s",
             job_dir.name, len(items), context.study_uid)
    return job_dir


@dataclass
class JobOutcome:
    store: scu.StoreResult
    resolved: bool
    counts: dict
    uncertain: int
    resumable: int
    job_dir: Path
    discarded: bool = False
    cancelled: bool = False


def send_job(job_dir: Path, my_aetitle: str, node: Node, *, tls_args=None,
             progress=_noop, on_frac=None, should_cancel=None,
             keep_copy: bool = False) -> JobOutcome:
    """Send the not-yet-stored objects of a job, updating the journal per object.

    Only PREPARED/REJECTED objects are (re)sent, reusing their original UIDs.
    UNCERTAIN objects are skipped (held for manual verification). The job folder
    is removed once every object is stored, unless ``keep_copy``.
    """
    job_dir = Path(job_dir)
    journal = read_journal(job_dir)
    by_name = {it["file"]: it for it in journal["items"]}
    to_send = [job_dir / it["file"] for it in journal["items"]
               if it["state"] in _RESUMABLE]

    def on_result(f: Path, state: str, code: str) -> None:
        it = by_name.get(Path(f).name)
        if it is not None:
            it["state"] = state
            it["code"] = code
            it["updated"] = _now()
            _write(job_dir, journal)       # persist incrementally (crash-safe)

    if to_send:
        result = scu.c_store(my_aetitle, node, to_send, progress=progress,
                             on_frac=on_frac, should_cancel=should_cancel,
                             tls_args=tls_args, on_result=on_result)
    else:
        result = scu.StoreResult()
    journal["last_attempt"] = _now()
    _write(job_dir, journal)

    # NOTE: C-STORE acceptance recorded above is not proof of durable archival.
    # DICOM Storage Commitment is intentionally not implemented in this client:
    # a reliable commitment result usually arrives asynchronously on a separate
    # association and so belongs in an authenticated intake gateway rather than a
    # desktop tool. The per-object journal states are the available evidence.
    #
    # Likewise, an attributable clinical audit trail (operator, destination,
    # patient/study, per-object outcome) is intentionally NOT written here: a
    # PHI-bearing audit log on the desktop is data-at-rest risk with little
    # benefit. Attribution belongs in a central audit repository (e.g. ATNA /
    # syslog) emitted by managed infrastructure, which this build does not have.
    c = counts(journal)
    resolved = is_resolved(journal)
    cancelled = bool(should_cancel and should_cancel())
    discarded = False
    if resolved and not keep_copy:
        discard_job(job_dir)
        discarded = True
    return JobOutcome(store=result, resolved=resolved, counts=c,
                      uncertain=c[UNCERTAIN], resumable=c[PREPARED] + c[REJECTED],
                      job_dir=job_dir, discarded=discarded, cancelled=cancelled)


def discard_job(job_dir: Path) -> None:
    shutil.rmtree(Path(job_dir), ignore_errors=True)


def unresolved_jobs() -> list[tuple[Path, dict]]:
    """Journaled jobs that are not fully stored (for startup recovery)."""
    out: list[tuple[Path, dict]] = []
    base = paths.jobs_dir()
    for d in sorted(base.glob("job_*")):
        if not (d / JOURNAL).exists():
            continue                       # not a journaled job; skip
        try:
            j = read_journal(d)
        except Exception as exc:           # noqa: BLE001
            log.warning("Unreadable journal in %s: %s", d.name, exc)
            continue
        if not is_resolved(j):
            out.append((d, j))
    return out
