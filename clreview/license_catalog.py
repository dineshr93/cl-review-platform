"""License map proposals: SL license names matched to PL licenses through the SPDX list.

Three sources, one rule (``domain.license_key``):
- SL: the short names the scanner actually reported (snapshots), with the tool's full name when known;
- PL: the tool's license catalogue, plus the names the BOMs use;
- SPDX: the official list, as the bridge (SL short name -> SPDX id -> PL license with that id or name).

Proposals are for a reviewer to accept; nothing here writes the map.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .domain import license_key, license_relation, norm_license

SPDX_URL = "https://spdx.org/licenses/licenses.json"
RECOMMEND_AT = 0.9  # pre-selected for the reviewer; below this a proposal is shown but not ticked

# method -> confidence; "in use" (the name appears in a PL BOM) adds a little to break ties
METHODS = {"spdx_id": 0.98, "exact_name": 0.97, "name_same": 0.9, "name_subset": 0.7}
IN_USE_BONUS = 0.01
# scanner verdicts that look like license names but name no license; nothing to map them to
NOT_A_LICENSE = frozenset(norm_license(n) for n in (
    "No_license_found", "NOASSERTION", "NONE", "Void", "See-file", "See-doc", "See-URL",
    "UnclassifiedLicense", "Dual-license"))


@dataclass
class Proposal:
    short_name: str
    spdx: str | None
    bd_license: str | None
    method: str
    confidence: float
    action: str  # add | update | unresolved
    current: str | None = None
    recommended: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _spdx_for(sl: dict[str, Any], spdx: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for value in (sl.get("spdx"), sl["name"]):
        if value and norm_license(value) in by_id:
            return by_id[norm_license(value)]
    keys = [license_key(v) for v in (sl.get("full_name"), sl["name"]) if v]
    hits = [s for s in spdx if any(license_relation(t, k) == "same" for k in keys for t in s["_keys"])]
    live = [s for s in hits if not s.get("deprecated")] or hits
    return live[0] if len(live) == 1 else None


def _score(sl: dict[str, Any], sp: dict[str, Any] | None, pl: dict[str, Any]) -> tuple[str, float] | None:
    if sp and pl.get("spdx") and norm_license(pl["spdx"]) == norm_license(sp["id"]):
        return "spdx_id", METHODS["spdx_id"]
    exact = {norm_license(sl["name"])} | ({norm_license(sp["id"])} if sp else set())
    if norm_license(pl["name"]) in exact:
        return "exact_name", METHODS["exact_name"]
    pk = pl["_key"]
    names = [sl["name"], sl.get("full_name")] + ([sp["id"], sp["name"]] if sp else [])
    relations = {license_relation(pk, license_key(n)) for n in names if n}
    if "same" in relations:
        return "name_same", METHODS["name_same"]
    if "subset" in relations:
        return "name_subset", METHODS["name_subset"]
    return None


def propose(sl_names: list[dict[str, Any]], pl_catalog: list[dict[str, Any]], spdx: list[dict[str, Any]],
            current: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One proposal per SL name that the map does not already settle.

    ``current`` is ``Service.license_map_lookup()``. A row the reviewer wrote (source ``manual``)
    is theirs and never proposed over; a seed or earlier suggestion is, when the tools disagree.
    An SL name that is already a PL license name needs no mapping and is skipped.
    """
    pl_catalog = [{**p, "_key": license_key(p["name"])} for p in pl_catalog]
    spdx = [{**s, "_keys": (license_key(s["id"]), license_key(s["name"]))} for s in spdx]
    by_id = {norm_license(s["id"]): s for s in spdx}
    pl_names = {norm_license(p["name"]) for p in pl_catalog}
    out: list[Proposal] = []
    skipped = {"manual": 0, "direct": 0, "unchanged": 0, "not_a_license": 0}
    for sl in sl_names:
        key = norm_license(sl["name"])
        if key in NOT_A_LICENSE:
            skipped["not_a_license"] += 1
            continue
        row = current.get(key)
        if row and row.get("source") == "manual":
            skipped["manual"] += 1
            continue
        if not row and key in pl_names:
            skipped["direct"] += 1
            continue
        sp = _spdx_for(sl, spdx, by_id)
        scored = []
        for pl in pl_catalog:
            hit = _score(sl, sp, pl)
            if hit:
                bonus = IN_USE_BONUS if pl.get("in_use") else 0.0
                scored.append({"bd_license": pl["name"], "method": hit[0],
                               "confidence": round(min(hit[1] + bonus, 0.99), 2)})
        scored.sort(key=lambda c: (-c["confidence"], c["bd_license"]))
        spdx_id = (sp or {}).get("id") or sl.get("spdx")
        if not scored:
            out.append(Proposal(sl["name"], spdx_id, None, "none", 0.0, "unresolved",
                                current=(row or {}).get("bd_license"),
                                note="no PL license matches this name or its SPDX identifier"))
            continue
        best = scored[0]
        tied = len(scored) > 1 and scored[1]["confidence"] == best["confidence"]
        if row and norm_license(row.get("bd_license")) == norm_license(best["bd_license"]):
            skipped["unchanged"] += 1
            continue
        out.append(Proposal(
            sl["name"], spdx_id, best["bd_license"], best["method"], best["confidence"],
            "update" if row else "add", current=(row or {}).get("bd_license"),
            recommended=best["confidence"] >= RECOMMEND_AT and not tied,
            candidates=scored[:5],
            note="several PL licenses match equally; pick one" if tied else ""))
    order = {"add": 0, "update": 1, "unresolved": 2}
    out.sort(key=lambda p: (order[p.action], -p.confidence, p.short_name.lower()))
    return {"items": [p.to_dict() for p in out], "skipped": skipped}


def parse_spdx(data: Any) -> list[dict[str, Any]]:
    """The official ``licenses.json`` shape, reduced to what matching reads."""
    return [{"id": x["licenseId"], "name": x.get("name") or x["licenseId"],
             "deprecated": bool(x.get("isDeprecatedLicenseId"))}
            for x in (data or {}).get("licenses", []) if x.get("licenseId")]
