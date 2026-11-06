"""Core data models.

``Node`` is intentionally field-compatible with DCMTools' ``model.Node`` so the
engine and any future toolkit tab can share destination records.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field

AE_TITLE_MAX = 16


def validate_aetitle(ae: str, *, allow_empty: bool = False) -> str:
    """Return '' if a DICOM AE Title is valid, else a short reason."""
    if ae is None or ae == "":
        return "" if allow_empty else "cannot be empty"
    if ae.strip() == "":
        return "cannot be all spaces"
    if len(ae) > AE_TITLE_MAX:
        return f"too long ({len(ae)} chars; max {AE_TITLE_MAX})"
    if "\\" in ae:
        return "backslash (\\) is not allowed"
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in ae):
        return "only printable ASCII is allowed (no control/non-ASCII chars)"
    return ""


@dataclass
class Node:
    """A remote DICOM peer (PACS / VNA / workstation)."""

    name: str
    aetitle: str
    host: str
    port: int
    note: str = ""
    timeout: int = 30
    tls: bool = False
    calling_aetitle: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(
            name=str(d.get("name", "")).strip(),
            aetitle=str(d.get("aetitle", "")).strip(),
            host=str(d.get("host", "")).strip(),
            port=int(d.get("port", 104) or 104),
            note=str(d.get("note", "")).strip(),
            timeout=int(d.get("timeout", 30) or 30),
            tls=bool(d.get("tls", False)),
            calling_aetitle=str(d.get("calling_aetitle", "")).strip(),
        )

    def calling(self, default: str) -> str:
        """The local (calling) AE Title to present for this node."""
        return self.calling_aetitle or default

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.name:
            problems.append("Name is empty.")
        ae_problem = validate_aetitle(self.aetitle)
        if ae_problem:
            problems.append(f"AE Title '{self.aetitle}': {ae_problem}.")
        calling_problem = validate_aetitle(self.calling_aetitle, allow_empty=True)
        if calling_problem:
            problems.append(f"Calling AE '{self.calling_aetitle}': {calling_problem}.")
        if not self.host:
            problems.append("Host is empty.")
        else:
            try:
                ipaddress.ip_address(self.host)
            except ValueError:
                if ".." in self.host or self.host.endswith("."):
                    problems.append(f"Host '{self.host}' looks malformed.")
        if not (1 <= int(self.port) <= 65535):
            problems.append(f"Port {self.port} is out of range (1-65535).")
        return problems

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.aetitle}@{self.host}:{self.port})"


@dataclass
class StudyContext:
    """Patient + study identity pulled from PACS by C-FIND, used to stamp the
    new Secondary Capture series onto an *existing* study."""

    patient_id: str = ""
    patient_name: str = ""
    birth_date: str = ""       # DICOM DA: YYYYMMDD
    sex: str = ""              # M / F / O
    study_uid: str = ""        # existing StudyInstanceUID (the key value)
    study_date: str = ""
    study_time: str = ""
    accession: str = ""
    study_id: str = ""
    study_description: str = ""
    referring_physician: str = ""
    modalities: str = ""       # ModalitiesInStudy (informational)

    @property
    def is_resolved(self) -> bool:
        return bool(self.study_uid)


@dataclass
class SeriesInfo:
    """Details for the NEW series that the images become."""

    modality: str = "OT"                 # Secondary Capture is typically OT
    series_description: str = "Print2PACS Import"
    body_part: str = ""
    series_number: int = 9901            # high number to avoid clashing
    conversion_type: str = "WSD"         # SC Equipment: WSD/SD/SI/DF/...


# Allowed SC Equipment ConversionType values (PS3.3 C.8.6.1).
CONVERSION_TYPES = {
    "DV": "Digitized Video",
    "DI": "Digital Interface",
    "DF": "Digitized Film",
    "WSD": "Workstation",
    "SD": "Scanned Document",
    "SI": "Scanned Image",
    "DRW": "Drawing",
    "SYN": "Synthetic Image",
}
