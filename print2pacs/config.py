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


def load_destinations() -> list[Node]:
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
