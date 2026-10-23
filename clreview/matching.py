"""Component matching across a project-level and a source-level inventory.

Hierarchy (highest confidence first):
1. exact normalized purl
2. exact normalized name + version
3. shared strong identifier
4. best-effort normalized name (one side lacks a version)
5. fuzzy name suggestion, always requiring reviewer confirmation

Nothing here merges components: it proposes matches with rationale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .domain import ComponentData, external_ids, norm_name, norm_purl, norm_version

AUTO_MATCH_METHODS = {"purl", "name_version", "strong_id", "name_only"}
FUZZY_MIN = 0.72


@dataclass
class MatchResult:
    pl_key: str
    sl_key: str
    method: str
    confidence: float
    rationale: list[str] = field(default_factory=list)
    mismatch_reasons: list[str] = field(default_factory=list)
    status: str = "auto"  # auto | suggested | unmatched
    pl_name: str = ""
    pl_version: str | None = None
    sl_name: str = ""
    sl_version: str | None = None


def _ids(component: ComponentData) -> set[str]:
    return set(external_ids(component.raw)) | set(external_ids(component.metadata))


def _name_version_reasons(pl: ComponentData, sl: ComponentData) -> list[str]:
    reasons: list[str] = []
    if norm_name(pl.name) != norm_name(sl.name):
        reasons.append(f"name differs: PL {pl.name!r} vs SL {sl.name!r}")
    if norm_version(pl.version) != norm_version(sl.version):
        reasons.append(f"version differs: PL {pl.version!r} vs SL {sl.version!r}")
    return reasons


def match_components(pl: list[ComponentData], sl: list[ComponentData]) -> list[MatchResult]:
    results: list[MatchResult] = []
    used_pl: set[str] = set()
    used_sl: set[str] = set()

    def add(plc: ComponentData, slc: ComponentData, method: str, confidence: float, rationale: list[str], status: str = "auto") -> None:
        used_pl.add(plc.external_id)
        used_sl.add(slc.external_id)
        results.append(
            MatchResult(
                pl_key=plc.external_id,
                sl_key=slc.external_id,
                method=method,
                confidence=round(confidence, 2),
                rationale=rationale,
                mismatch_reasons=_name_version_reasons(plc, slc),
                status=status,
                pl_name=plc.name,
                pl_version=plc.version,
                sl_name=slc.name,
                sl_version=slc.version,
            )
        )

    # 1. purl
    sl_by_purl: dict[str, list[ComponentData]] = {}
    for c in sl:
        p = norm_purl(c.purl)
        if p:
            sl_by_purl.setdefault(p, []).append(c)
    for plc in pl:
        p = norm_purl(plc.purl)
        if not p or plc.external_id in used_pl:
            continue
        for slc in sl_by_purl.get(p, []):
            if slc.external_id in used_sl:
                continue
            add(plc, slc, "purl", 0.99, [f"identical purl: {p}"])
            break

    # 2. name + version
    sl_by_nv: dict[tuple[str, str], list[ComponentData]] = {}
    for c in sl:
        if c.external_id in used_sl:
            continue
        sl_by_nv.setdefault((norm_name(c.name), norm_version(c.version)), []).append(c)
    for plc in pl:
        if plc.external_id in used_pl:
            continue
        key = (norm_name(plc.name), norm_version(plc.version))
        for slc in sl_by_nv.get(key, []):
            if slc.external_id in used_sl:
                continue
            add(plc, slc, "name_version", 0.95, ["normalized name and version are identical"])
            break

    # 3. strong identifier
    sl_by_id: dict[str, list[ComponentData]] = {}
    for c in sl:
        if c.external_id in used_sl:
            continue
        for i in _ids(c):
            sl_by_id.setdefault(i, []).append(c)
    for plc in pl:
        if plc.external_id in used_pl:
            continue
        for i in _ids(plc):
            for slc in sl_by_id.get(i, []):
                if slc.external_id in used_sl:
                    continue
                add(plc, slc, "strong_id", 0.9, [f"shared identifier: {i}"])
                break
            if plc.external_id in used_pl:
                break

    # 4. name only, version missing on one side
    sl_by_name: dict[str, list[ComponentData]] = {}
    for c in sl:
        if c.external_id in used_sl:
            continue
        sl_by_name.setdefault(norm_name(c.name), []).append(c)
    for plc in pl:
        if plc.external_id in used_pl:
            continue
        candidates = [c for c in sl_by_name.get(norm_name(plc.name), []) if c.external_id not in used_sl]
        if len(candidates) == 1 and (not plc.version or not candidates[0].version):
            add(plc, candidates[0], "name_only", 0.72, ["name matches; version absent on one side"])
        elif len(candidates) == 1 and norm_version(plc.version) == norm_version(candidates[0].version):
            add(plc, candidates[0], "name_only", 0.72, ["name matches; versions agree"])

    # 5. fuzzy suggestion (never auto)
    remaining_pl = [c for c in pl if c.external_id not in used_pl]
    remaining_sl = [c for c in sl if c.external_id not in used_sl]
    for plc in remaining_pl:
        best = None
        best_ratio = 0.0
        for slc in remaining_sl:
            if slc.external_id in used_sl:
                continue
            ratio = SequenceMatcher(None, norm_name(plc.name), norm_name(slc.name)).ratio()
            if ratio > best_ratio:
                best, best_ratio = slc, ratio
        if best is not None and best_ratio >= FUZZY_MIN:
            add(
                plc,
                best,
                "fuzzy",
                min(0.69, 0.40 + best_ratio * 0.30),
                [f"fuzzy name similarity {best_ratio:.2f}; reviewer confirmation required"],
                status="suggested",
            )

    # leftovers stay explicitly unmatched
    for plc in pl:
        if plc.external_id not in used_pl:
            results.append(
                MatchResult(pl_key=plc.external_id, sl_key="", method="unmatched", confidence=0.0,
                            rationale=["no SL counterpart found"], status="unmatched",
                            pl_name=plc.name, pl_version=plc.version)
            )
    for slc in sl:
        if slc.external_id not in used_sl:
            results.append(
                MatchResult(pl_key="", sl_key=slc.external_id, method="unmatched", confidence=0.0,
                            rationale=["no PL counterpart found"], status="unmatched",
                            sl_name=slc.name, sl_version=slc.version)
            )
    return results
