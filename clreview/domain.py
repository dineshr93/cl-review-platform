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
    "interrupted",
    "skipped",
    "unsupported",
]
ITEM_STATUSES = [
    "pending",
    "dry_run",
    "succeeded",
    "failed",
    "skipped_already_applied",
    "skipped_prerequisite",
    "unsupported_target",
    "conflict",
    "stale",
    "interrupted",
]

#: Item statuses `retry()` may pick up again. `interrupted` is here because a live job runs
#: inside the request that created it: if the process dies between two items, the items that
#: were never attempted are not failures, but they are the only thing worth running again.
RETRYABLE_ITEM_STATUSES = frozenset({"failed", "interrupted"})

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
FIELD_REVIEW_STATUS = "review_status"
FIELD_UPLOAD_STATUS = "upload_status"
# a sync-only field: a rejected PL-only copyright notice is deactivated in the PL tool, never deleted
FIELD_COPYRIGHT_DEACTIVATE = "copyright_deactivate"
FIELDS = [FIELD_COPYRIGHT, FIELD_LICENSE_NAME, FIELD_LICENSE_TEXT]
FINAL_FIELDS = [FIELD_REVIEW_STATUS, FIELD_UPLOAD_STATUS]
# every field that writes component data to the PL tool (the final fields wait on these)
PL_DATA_FIELDS = (FIELD_COPYRIGHT, FIELD_COPYRIGHT_DEACTIVATE, FIELD_LICENSE_NAME, FIELD_LICENSE_TEXT)

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
    write_review_status: bool = False
    write_upload_status: bool = False
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
    """`libfoo-1.2.3` -> ('libfoo', '1.2.3'); `bar` -> ('bar', None).

    A text qualifier is part of the version (`guava-31.1-jre` -> '31.1-jre'), because registry
    versions carry them (`-jre`, `-alpha10`, `-1`). Archive suffixes are still ignored.
    """
    base = _clean(name).rstrip("/")
    qualified = re.match(r"^(?P<n>.+?)[-_]v?(?P<v>\d+(?:\.\d+){0,3}[-_+][A-Za-z][-\w]*)$", base)
    if qualified:
        return qualified.group("n"), qualified.group("v")
    m = re.match(r"^(?P<n>.+?)[-_]v?(?P<v>\d+(?:\.\d+){0,3})(?:[-_.].*)?$", base)
    if m:
        return m.group("n"), m.group("v")
    return base, None


NameKey = tuple[frozenset[str], str]


def name_key(name: str, version: str | None = None) -> NameKey:
    """The one name rule every comparison uses: the name's letter runs, and its version.

    Numbers are split off the text: ``libpng16`` and ``libpng`` share the text ``{libpng}``, so the
    version decides. A name that carries its version and has none of its own gives it up:
    (``zlib-1.2.11``, None) -> ({zlib}, "1.2.11"), which is how FOSSology names uploads.
    """
    text, ver = name or "", version
    if not norm_version(ver):
        base, carried = split_component_dir(text)
        if carried:
            text, ver = base, carried
    return frozenset(re.findall(r"[^\W\d_]+", _clean(text).lower())), norm_version(ver)


def name_relation(pl: NameKey, sl: NameKey) -> str | None:
    """``same`` when the texts are identical, ``subset`` when SL's text is a strict subset of PL's.

    Only that direction: a scanner reports the short name of what it found (``zlib``), the project
    list the full one (``zlib-ng``), never the reverse. Versions are compared by the caller.
    """
    if not sl[0] or not pl[0]:
        return None
    if sl[0] == pl[0]:
        return "same"
    if sl[0] < pl[0]:
        return "subset"
    return None


LICENSE_NOISE = frozenset({"v", "version", "the", "license", "licence"})
LicenseKey = tuple[frozenset[str], tuple[str, ...]]


def license_key(name: str) -> LicenseKey:
    """The name rule for license names: letter runs, and the numbers in order.

    Unlike a component, a license's numbers are its identity (GPL 2 is not GPL 3), so they must be
    equal rather than compared as a version. ``2.0`` equals ``2``, ``+`` reads as "or later", and
    filler words drop: ``Apache License, Version 2.0`` and ``Apache-2.0`` share one key.
    """
    s = _clean(name or "").lower().replace("+", " or later ")
    words = frozenset(w for w in re.findall(r"[^\W\d_]+", s) if w not in LICENSE_NOISE)
    nums = tuple(re.sub(r"(?:\.0)+$", "", n) for n in re.findall(r"\d+(?:\.\d+)*", s))
    return words, nums


def license_relation(pl: LicenseKey, sl: LicenseKey) -> str | None:
    """``same`` or ``subset`` (SL's words a strict subset of PL's), only when the numbers agree."""
    if pl[1] != sl[1]:
        return None
    return name_relation((pl[0], ""), (sl[0], ""))


def query_matches(query: str, name: str, version: str | None, haystack: str = "") -> bool:
    """A search box hit: a plain substring of the haystack, or the name rule.

    The name rule makes ``libpng16`` find SL ``libpng 1.6.37`` and ``zlib 1.2`` find
    ``zlib-1.2.11``: every letter run of the query is in the name, and the query's digits, glued
    or separate, start the component's version digits (so ``lib287`` does not find ``lib12``).
    """
    q = (query or "").strip().lower()
    if not q:
        return True
    if q in f"{name or ''} {version or ''} {haystack}".lower():
        return True
    words = name_key(q.replace(" ", "-"))[0]
    text, ver = name_key(name, version)
    if not words or not words <= text:
        return False
    qdigits = "".join(re.findall(r"\d+", q))
    return not qdigits or "".join(re.findall(r"\d+", ver)).startswith(qdigits)


def name_similarity(pl: NameKey, sl: NameKey) -> float:
    """0..1 ranking under the name rule, for candidate lists (never for an automatic match)."""
    relation = name_relation(pl, sl)
    same_version = bool(pl[1]) and pl[1] == sl[1]
    if relation == "same":
        return 1.0 if same_version else 0.9
    if relation == "subset":
        return 0.8 if same_version else 0.7
    union = pl[0] | sl[0]
    return round(0.6 * len(pl[0] & sl[0]) / len(union), 2) if union else 0.0


def pair_by_name(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{current external_id: previous row} - by key first, then by the same name text.

    Rows carry ``external_id``, ``name`` and ``version``. A component whose version moved has a new
    key but the same name text, and pairs with the previous row at the most similar version.
    """
    by_key = {r["external_id"]: r for r in previous}
    out = {r["external_id"]: by_key[r["external_id"]] for r in current if r["external_id"] in by_key}
    taken = {p["external_id"] for p in out.values()}
    by_text: dict[frozenset[str], list[tuple[dict[str, Any], str]]] = {}
    for r in previous:
        if r["external_id"] not in taken:
            text, ver = name_key(r["name"], r["version"])
            if text:
                by_text.setdefault(text, []).append((r, ver))
    for r in current:
        if r["external_id"] in out:
            continue
        text, ver = name_key(r["name"], r["version"])
        options = [(p, v) for p, v in by_text.get(text, []) if p["external_id"] not in taken]
        if options:
            prior = max(options, key=lambda o: _shared_version_prefix(ver, o[1]))[0]
            out[r["external_id"]] = prior
            taken.add(prior["external_id"])
    return out


def _shared_version_prefix(a: str, b: str) -> int:
    ta, tb = re.findall(r"[a-z]+|\d+", a), re.findall(r"[a-z]+|\d+", b)
    n = 0
    for x, y in zip(ta, tb):
        if x != y:
            break
        n += 1
    return n


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
