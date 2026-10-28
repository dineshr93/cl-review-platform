"""Comparison services: copyright notices, license names, license-text provenance.

Comparison never decides which side is right; it classifies the difference and
keeps both raw values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .domain import (
    KIND_GENERIC,
    KIND_SOURCE,
    KIND_UNKNOWN,
    CopyrightEntry,
    LicenseEntry,
    LicenseTextCandidate,
    copyright_key,
    is_license_file,
    license_file_score,
    norm_copyright,
    norm_license,
    sha256,
)


@dataclass
class CopyrightRow:
    key: str
    kind: str  # matched | variant | pl_only | sl_only | duplicate
    pl_text: str | None = None
    sl_text: str | None = None
    note: str = ""


def compare_copyrights(pl: list[CopyrightEntry], sl: list[CopyrightEntry]) -> list[CopyrightRow]:
    """Pair notices by normalized text; duplicates and formatting variants are surfaced."""
    rows: list[CopyrightRow] = []
    pl_index: dict[str, list[CopyrightEntry]] = {}
    sl_index: dict[str, list[CopyrightEntry]] = {}
    for e in pl:
        if e.active:
            pl_index.setdefault(copyright_key(e.text), []).append(e)
    for e in sl:
        if e.active:
            sl_index.setdefault(copyright_key(e.text), []).append(e)

    for key, pl_entries in pl_index.items():
        sl_entries = sl_index.get(key, [])
        if not sl_entries:
            continue  # handled below, once, as one-sided or variant
        for i, e in enumerate(pl_entries):
            other = sl_entries[i] if i < len(sl_entries) else sl_entries[0]
            same_text = re.sub(r"\s+", " ", e.text).strip() == re.sub(r"\s+", " ", other.text).strip()
            rows.append(
                CopyrightRow(
                    key=key,
                    kind="matched" if same_text else "variant",
                    pl_text=e.text,
                    sl_text=other.text,
                    note="" if same_text else "formatting/case variation only",
                )
            )
        for extra in pl_entries[len(sl_entries):]:
            rows.append(CopyrightRow(key=key, kind="duplicate", pl_text=extra.text, note="duplicate within PL"))
        for extra in sl_entries[len(pl_entries):]:
            rows.append(CopyrightRow(key=key, kind="duplicate", sl_text=extra.text, note="duplicate within SL"))

    # leftover entries are paired as likely formatting variants when the text is very close
    leftover_pl = [e for e in pl if e.active and copyright_key(e.text) not in sl_index]
    leftover_sl = [e for e in sl if e.active and copyright_key(e.text) not in pl_index]
    from difflib import SequenceMatcher
    for p_entry in list(leftover_pl):
        best, best_ratio = None, 0.0
        for s_entry in leftover_sl:
            ratio = SequenceMatcher(None, norm_copyright(p_entry.text), norm_copyright(s_entry.text)).ratio()
            if ratio > best_ratio:
                best, best_ratio = s_entry, ratio
        if best is not None and best_ratio >= 0.92:
            leftover_pl.remove(p_entry)
            leftover_sl.remove(best)
            rows.append(CopyrightRow(key=copyright_key(p_entry.text), kind="variant",
                                     pl_text=p_entry.text, sl_text=best.text,
                                     note="formatting/case variation only"))
    for e in leftover_pl:
        rows.append(CopyrightRow(key=copyright_key(e.text), kind="pl_only", pl_text=e.text, note="present only in PL"))
    for e in leftover_sl:
        rows.append(CopyrightRow(key=copyright_key(e.text), kind="sl_only", sl_text=e.text, note="present only in SL"))

    order = {"variant": 0, "sl_only": 1, "pl_only": 2, "duplicate": 3, "matched": 4}
    return sorted(rows, key=lambda r: (order.get(r.kind, 9), r.key))


@dataclass
class LicenseRow:
    key: str
    kind: str  # matched | pl_only | sl_only | spdx_diff
    pl_name: str | None = None
    sl_name: str | None = None
    pl_spdx: str | None = None
    sl_spdx: str | None = None
    license_type: str = "declared"
    evidence_paths: list[str] = field(default_factory=list)
    note: str = ""


def compare_licenses(pl: list[LicenseEntry], sl: list[LicenseEntry]) -> list[LicenseRow]:
    rows: list[LicenseRow] = []
    pl_index: dict[str, list[LicenseEntry]] = {}
    sl_index: dict[str, list[LicenseEntry]] = {}
    for e in pl:
        if e.active:
            pl_index.setdefault(norm_license(e.name), []).append(e)
    for e in sl:
        if e.active:
            sl_index.setdefault(norm_license(e.name), []).append(e)

    for key, pl_entries in pl_index.items():
        sl_entries = sl_index.get(key, [])
        if not sl_entries:
            for e in pl_entries:
                rows.append(
                    LicenseRow(key=key, kind="pl_only", pl_name=e.name, pl_spdx=e.spdx,
                               license_type=e.license_type, note="declared in PL only")
                )
            continue
        for i, e in enumerate(pl_entries):
            other = sl_entries[i] if i < len(sl_entries) else sl_entries[0]
            kind = "matched"
            note = ""
            if (e.spdx or "") != (other.spdx or ""):
                kind = "spdx_diff"
                note = "same name, different SPDX identifier"
            rows.append(
                LicenseRow(key=key, kind=kind, pl_name=e.name, sl_name=other.name,
                           pl_spdx=e.spdx, sl_spdx=other.spdx, license_type=other.license_type,
                           evidence_paths=list(other.evidence_paths), note=note)
            )
        for extra in pl_entries[len(sl_entries):]:
            rows.append(LicenseRow(key=key, kind="pl_only", pl_name=extra.name, pl_spdx=extra.spdx,
                                   license_type=extra.license_type, note="additional PL declaration"))

    for key, sl_entries in sl_index.items():
        if key in pl_index:
            continue
        for e in sl_entries:
            rows.append(
                LicenseRow(key=key, kind="sl_only", sl_name=e.name, sl_spdx=e.spdx,
                           license_type=e.license_type, evidence_paths=list(e.evidence_paths),
                           note="detected in SL only")
            )
    return rows


@dataclass
class TextCandidateView:
    side: str  # pl | sl
    kind: str
    provenance: str
    file_name: str | None
    file_path: str | None
    content_hash: str | None
    is_license_file: bool
    score: int
    text: str
    preferred: bool = False


@dataclass
class LicenseTextComparison:
    pl_current: TextCandidateView | None = None
    candidates: list[TextCandidateView] = field(default_factory=list)
    preferred: TextCandidateView | None = None
    recommendation: str = "needs_review"  # needs_review | replace_with_source | already_matches | no_source_text
    reasons: list[str] = field(default_factory=list)


def _view(side: str, c: LicenseTextCandidate) -> TextCandidateView:
    return TextCandidateView(
        side=side,
        kind=c.kind,
        provenance=c.provenance,
        file_name=c.file_name,
        file_path=c.file_path,
        content_hash=c.content_hash or sha256(c.text),
        is_license_file=c.is_license_file or bool(c.file_name and is_license_file(c.file_name)),
        score=license_file_score(c.file_name or ""),
        text=c.text,
    )


def compare_license_texts(
    pl_texts: list[LicenseTextCandidate], sl_texts: list[LicenseTextCandidate]
) -> LicenseTextComparison:
    """Separate generic metadata text from source-file-derived text.

    Only an identified, relevant license file can be proposed for synchronization.
    """
    out = LicenseTextComparison()
    pl_views = [_view("pl", c) for c in pl_texts]
    sl_views = [_view("sl", c) for c in sl_texts]
    out.candidates = pl_views + sl_views
    if pl_views:
        out.pl_current = pl_views[0]

    source_candidates = [v for v in sl_views if v.kind == KIND_SOURCE and v.is_license_file]
    if not source_candidates:
        unknown = [v for v in sl_views if v.kind == KIND_UNKNOWN]
        out.reasons.append("no identified source-file license text in SL evidence")
        if unknown:
            out.reasons.append("unverified text present but provenance is unknown")
        out.recommendation = "no_source_text"
        return out

    source_candidates.sort(key=lambda v: (-v.score, len(v.file_path or ""), v.file_name or ""))
    out.preferred = source_candidates[0]
    out.preferred.preferred = True

    if out.pl_current and out.pl_current.content_hash == out.preferred.content_hash:
        out.recommendation = "already_matches"
        out.reasons.append("PL text hash matches the preferred source file")
    elif out.pl_current and out.pl_current.kind == KIND_GENERIC:
        out.recommendation = "replace_with_source"
        out.reasons.append("PL currently stores generic license metadata text")
        out.reasons.append(f"source file {out.preferred.file_path or out.preferred.file_name} provides component-specific text")
    else:
        out.recommendation = "replace_with_source"
        out.reasons.append("source-file text differs from the text stored in PL")
    if len(source_candidates) > 1:
        out.reasons.append(f"{len(source_candidates)} candidate license files; reviewer selects the relevant one")
    return out


__all__ = [
    "CopyrightRow",
    "LicenseRow",
    "TextCandidateView",
    "LicenseTextComparison",
    "compare_copyrights",
    "compare_licenses",
    "compare_license_texts",
    "norm_copyright",
]
