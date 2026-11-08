"""Load/save settings and the destinations address book.

Real destinations (AE titles, hosts, ports) live only in the local, git-ignored
data folder - never in the repo.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import paths
from .logging_setup import get_logger
from .model import Node

log = get_logger("config")


@dataclass
class Settings:
    my_aetitle: str = "PRINT2PACS"
    appearance: str = "System"          # System / Light / Dark
    pdf_dpi: int = 200
    keep_sent_copy: bool = False        # debug: keep the .dcm files we send
    image_compression: str = "none"     # none | rle  (RLE Lossless)
    strip_private_tags: bool = False    # drop private/vendor tags from imported DICOM
    # Remembered destinations by Node.name (query source + send target may differ).
    query_destination: str = ""
    send_destination: str = ""
    # Defaults pre-filled into the series step (editable per job).
    default_modality: str = "Auto (by type)"   # or OT/SC/XC/... to force one
    default_series_description: str = "Print2PACS Import"
    default_conversion_type: str = "WSD"
    default_series_number: int = 9901
    # Workload limits (preflight): a job over any of these is refused, with a
    # message, before heavy conversion. Editable here to raise for a real job.
    max_pages_per_job: int = 300            # images + PDF/TIFF pages
    max_video_seconds: int = 900            # 15 min per video clip
    max_video_mb: int = 2048                # ~2 GB per encoded video object

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        base = cls()
        for k in base.__dataclass_fields__:
            if k in d and d[k] is not None:
                setattr(base, k, d[k])
        return base


def load_settings() -> Settings:
    f = paths.settings_file()
    if not f.exists():
        return Settings()
    try:
        return Settings.from_dict(json.loads(f.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read settings (%s); using defaults.", exc)
        return Settings()


def save_settings(settings: Settings) -> None:
    try:
        paths.settings_file().write_text(
            json.dumps(asdict(settings), indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not save settings: %s", exc)


# Admin-owned destination policy. When this file exists beside the exe (install
# dir, not the user-writable data folder), it is the authoritative destination
# list and the in-app editor is locked. The app only reads it.
POLICY_FILENAME = "destinations.policy.json"


def policy_path():
    return paths.resource_dir() / POLICY_FILENAME


def load_destination_policy() -> dict | None:
    """Return the admin destination policy dict, or None if absent/invalid."""
    f = policy_path()
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("destinations"), list):
            return data
        log.warning("Destination policy is malformed; ignoring it.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read destination policy (%s); ignoring it.", exc)
    return None


def destinations_are_managed() -> bool:
    return load_destination_policy() is not None


def approved_pairs(policy: dict | None = None) -> set | None:
    """Set of approved (query_name, send_name) pairs, or None if the policy
    defines none (caller then falls back to a confirmation)."""
    policy = policy if policy is not None else load_destination_policy()
    if not policy:
        return None
    pairs = policy.get("approved_pairs")
    if not pairs:
        return None
    return {(str(p.get("query", "")), str(p.get("send", ""))) for p in pairs}


def pair_allowed(query_name: str, send_name: str,
                 policy: dict | None = None):
    """True/False when the policy defines approved pairs; None when it doesn't."""
    pairs = approved_pairs(policy)
    if pairs is None:
        return None
    return (query_name, send_name) in pairs


def export_policy(destinations: list[Node], path, *, pairs=None) -> None:
    """Write a starter policy file from the given destinations."""
    data = {"managed": True,
            "destinations": [n.to_dict() for n in destinations]}
    if pairs:
        data["approved_pairs"] = [{"query": q, "send": s} for q, s in pairs]
    from pathlib import Path
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_destinations() -> list[Node]:
    policy = load_destination_policy()
    if policy is not None:
        return [Node.from_dict(d) for d in policy.get("destinations", [])]
    f = paths.destinations_file()
    if not f.exists():
        return []
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        return [Node.from_dict(d) for d in raw]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read destinations (%s).", exc)
        return []


def save_destinations(nodes: list[Node]) -> None:
    try:
        paths.destinations_file().write_text(
            json.dumps([n.to_dict() for n in nodes], indent=2),
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not save destinations: %s", exc)
