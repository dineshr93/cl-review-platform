"""Domain models, statuses and normalization shared by every layer.

Raw observations, normalized comparison values and approved values are
deliberately separate: nothing here mutates a scanner observation.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# --- review / sync lifecycle ------------------------------------------------

REVIEW_STATUSES = [
    "not_reviewed",
    "review_required",
    "in_progress",
    "reviewed",
    "approved_for_sync",
]
SYNC_STATUSES = [
    "not_started",
    "pending",
    "dry_run",
    "in_progress",
    "synchronized",
    "failed",
    "skipped",
    "unsupported",
]
ITEM_STATUSES = [
    "pending",
    "dry_run",
    "succeeded",
    "failed",
    "skipped_already_applied",
    "unsupported_target",
    "conflict",
    "stale",
]

# --- review actions ---------------------------------------------------------

ACCEPT_PL = "accept_pl"
APPROVE_SL = "approve_sl"
CORRECT = "correct"
REJECT = "reject"
FALSE_POSITIVE = "mark_false_positive"
IRRELEVANT = "mark_irrelevant"
NOTE_ONLY = "note_only"

ACTIONS = [ACCEPT_PL, APPROVE_SL, CORRECT, REJECT, FALSE_POSITIVE, IRRELEVANT, NOTE_ONLY]
SYNC_ACTIONS = {APPROVE_SL, CORRECT}
REJECTION_ACTIONS = {REJECT, FALSE_POSITIVE, IRRELEVANT}

FIELD_COPYRIGHT = "copyright"
FIELD_LICENSE_NAME = "license_name"
FIELD_LICENSE_TEXT = "license_text"
FIELDS = [FIELD_COPYRIGHT, FIELD_LICENSE_NAME, FIELD_LICENSE_TEXT]

KIND_GENERIC = "generic_metadata"
KIND_SOURCE = "source_file"
KIND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Capabilities:
    """What a connector can actually do. Never assume a write is possible."""

    read_components: bool = True
    read_copyrights: bool = True
    read_license_text: bool = True
    write_copyrights: bool = False
    write_license_names: bool = False
    write_license_text: bool = False
    verify_writes: bool = False
    notes: str = ""


@dataclass
class CopyrightEntry:
    text: str
    origin: str
    source_path: str | None = None
    active: bool = True
    retrieved_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LicenseEntry:
    name: str
    spdx: str | None = None
    license_type: str = "declared"  # declared | conjunctive | alternative
    evidence_paths: list[str] = field(default_factory=list)
    active: bool = True
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LicenseTextCandidate:
    kind: str  # generic_metadata | source_file | unknown
    text: str
    provenance: str
    file_name: str | None = None
    file_path: str | None = None
    content_hash: str | None = None
    is_license_file: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComponentData:
    external_id: str
    name: str
    version: str | None = None
    purl: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    copyrights: list[CopyrightEntry] = field(default_factory=list)
    licenses: list[LicenseEntry] = field(default_factory=list)
    license_texts: list[LicenseTextCandidate] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    write_key: str | None = None  # target handle used for PL writes, when the API needs one
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectInfo:
    external_id: str
    name: str
    href: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanInfo:
    external_id: str
    name: str
    date: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# --- normalization ----------------------------------------------------------


def _clean(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip()


def norm_name(value: str) -> str:
    """Case, punctuation and whitespace differences are not meaningful."""
    return re.sub(r"[\s._\-]+", " ", _clean(value).lower()).strip()


def norm_version(value: str | None) -> str:
    """Keep version distinctions; only strip noise like a leading 'v'."""
    v = _clean(value or "").lower()
    v = re.sub(r"^v(?=\d)", "", v)
    return re.sub(r"\s+", "", v)


def norm_purl(value: str | None) -> str | None:
    if not value:
        return None
    p = _clean(value).lower().rstrip("/")
    return p or None


def norm_license(value: str | None) -> str:
    """Exact-name comparison only: similar names are NOT equivalent."""
    return re.sub(r"\s+", " ", _clean(value or "")).upper()


def norm_copyright(value: str) -> str:
    """Whitespace/case-insensitive form used for duplicate detection."""
    return re.sub(r"\s+", " ", _clean(value)).strip().lower()


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def copyright_key(value: str) -> str:
    return sha256(norm_copyright(value))


LICENSE_FILE_RE = re.compile(
    r"^(licen[cs]e|copying|notice|copyright|unlicense)([-._].*)?$", re.IGNORECASE
)
LICENSE_FILE_EXT_RE = re.compile(r".*\.(licen[cs]e|copying|notice)$", re.IGNORECASE)


def is_license_file(name: str) -> bool:
    base = _clean(name).replace("\\", "/").rsplit("/", 1)[-1]
    return bool(base and (LICENSE_FILE_RE.match(base) or LICENSE_FILE_EXT_RE.match(base)))


def license_file_score(name: str) -> int:
    """Rank typical license files; used to propose, never to auto-approve."""
    base = _clean(name).replace("\\", "/").rsplit("/", 1)[-1].upper()
    stem = base.split(".", 1)[0]
    return {"LICENSE": 100, "LICENCE": 95, "COPYING": 90, "UNLICENSE": 85, "NOTICE": 70, "COPYRIGHT": 40}.get(
        stem, 30
    ) + (5 if base == stem else 0)


def split_component_dir(name: str) -> tuple[str, str | None]:
    """`libfoo-1.2.3` -> ('libfoo', '1.2.3'); `bar` -> ('bar', None)."""
    base = _clean(name).rstrip("/")
    m = re.match(r"^(?P<n>.+?)[-_]v?(?P<v>\d+(?:\.\d+){0,3})(?:[-_.].*)?$", base)
    if m:
        return m.group("n"), m.group("v")
    return base, None


def find_purl(raw: Any) -> str | None:
    """Look for a package URL anywhere in a vendor payload."""
    stack: list[Any] = [raw]
    seen = 0
    while stack and seen < 500:
        seen += 1
        node = stack.pop()
        if isinstance(node, str):
            if node.startswith("pkg:") and "/" in node:
                return norm_purl(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def external_ids(raw: Any) -> list[str]:
    """Collect identifier-ish strings from metadata for strong-match evidence."""
    out: list[str] = []
    for key in ("external_ids", "externalIds", "inputExternalIds", "identifiers"):
        val = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    for k in ("id", "idName", "value", "externalId"):
                        if isinstance(item.get(k), str):
                            out.append(item[k])
        elif isinstance(val, str):
            out.append(val)
    return [i.strip() for i in out if i and i.strip()]
