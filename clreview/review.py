"""Review workflow: queue, component workspace, field-level decisions, notes, statuses."""
from __future__ import annotations

import csv
import html
import io
import json
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from fnmatch import fnmatch
from typing import Any

from .comparison import compare_copyrights, compare_license_texts, compare_licenses
from .db import DB
from .domain import (
    ACCEPT_PL,
    ACTIONS,
    APPROVE_SL,
    CORRECT,
    FIELD_COPYRIGHT,
    FIELD_LICENSE_NAME,
    FIELD_LICENSE_TEXT,
    FIELDS,
    FALSE_POSITIVE,
    KIND_SOURCE,
    NOTE_ONLY,
    REJECTION_ACTIONS,
    copyright_key,
    norm_license,
    norm_name,
)
from .service import IN_CHUNK, Service, _ce, _le, _te, now


def _json(row: dict[str, Any], key: str) -> list[Any]:
    return json.loads(row.get(key) or "[]")


def _decision_rows(db: DB, match_id: int) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM decisions WHERE match_id=? ORDER BY field, entry_key", (match_id,))


def decisions_by_match(db: DB, match_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Decisions for many matches in one query per chunk, in the order _decision_rows uses."""
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in match_ids}
    for chunk in [match_ids[i:i + IN_CHUNK] for i in range(0, len(match_ids), IN_CHUNK)]:
        marks = ",".join("?" * len(chunk))
        rows = db.query(f"SELECT * FROM decisions WHERE match_id IN ({marks}) ORDER BY field, entry_key", tuple(chunk))
        for row in rows:
            out[row["match_id"]].append(row)
    return out


@dataclass
class RecomputeContext:
    """Rows already read for one call frame, so recompute costs no reads of its own."""

    match: dict[str, Any]
    decisions: list[dict[str, Any]]
    inventory: dict[str, dict[str, dict[str, Any]]]
    license_map: dict[str, dict[str, Any]] | None = None
    rules: list[dict[str, Any]] | None = None


class ReviewService:
    def __init__(self, service: Service):
        self.s = service
        self.db: DB = service.db

    # --- lookups ------------------------------------------------------------
    def match(self, match_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM matches WHERE id=?", (match_id,))
        if not row:
            raise KeyError(f"match {match_id} not found")
        row["rationale"] = _json(row, "rationale_json")
        row["mismatch_reasons"] = _json(row, "mismatch_json")
        return row

    def _component(self, mapping_id: int, side: str, key: str,
                   inventory: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
        if not key:
            return None
        if inventory is not None:
            return inventory.get(side, {}).get(key)
        row = self.s.component_by_key(mapping_id, side, key)
        if not row:
            return None
        obs = self.s.component_observations(row["id"])
        return {"component": row, **obs}

    def _active_rules(self, match: dict[str, Any], cached: list[dict[str, Any]] | None = None
                      ) -> list[dict[str, Any]]:
        if cached is None:
            return self.db.query(
                "SELECT * FROM false_positive_rules WHERE revoked_at IS NULL AND "
                "(scope='global' OR (scope='version' AND mapping_id=?) OR (scope='component' AND match_id=?)) "
                "ORDER BY id",
                (match["mapping_id"], match["id"]),
            )
        return [r for r in cached if r["scope"] == "global"
                or (r["scope"] == "version" and r["mapping_id"] == match["mapping_id"])
                or (r["scope"] == "component" and r["match_id"] == match["id"])]

    def _rule_applies(self, row: Any, rules: list[dict[str, Any]]) -> bool:
        if row.kind != "sl_only":
            return False
        license = norm_license(row.sl_name)
        for rule in rules:
            if norm_license(rule["license_name"]) != license:
                continue
            paths = row.evidence_paths or [""]
            if any(fnmatch(path, rule["path_pattern"] or "*") for path in paths):
                return True
        return False

    def _mark_auto_false_positives(self, match: dict[str, Any], license_rows: list[Any],
                                  cached_rules: list[dict[str, Any]] | None = None) -> None:
        rules = self._active_rules(match, cached_rules)
        for row in license_rows:
            if self._rule_applies(row, rules):
                row.auto_false_positive = True

    def comparison(self, match: dict[str, Any], inventory: dict | None = None,
                   *, license_map: dict[str, dict[str, Any]] | None = None,
                   rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        mid = match["mapping_id"]
        pl = self._component(mid, "pl", match["pl_key"], inventory)
        sl = self._component(mid, "sl", match["sl_key"], inventory)
        pl_c = [_ce(r) for r in (pl or {}).get("copyrights", [])]
        sl_c = [_ce(r) for r in (sl or {}).get("copyrights", [])]
        pl_l = [_le(r) for r in (pl or {}).get("licenses", [])]
        sl_l = [_le(r) for r in (sl or {}).get("licenses", [])]
        pl_t = [_te(r) for r in (pl or {}).get("license_texts", [])]
        sl_t = [_te(r) for r in (sl or {}).get("license_texts", [])]
        copyright_rows = compare_copyrights(pl_c, sl_c) if (pl_c or sl_c) else []
        lookup = license_map if license_map is not None else self.s.license_map_lookup()
        license_rows = compare_licenses(pl_l, sl_l, lookup) if (pl_l or sl_l) else []
        text_cmp = compare_license_texts(pl_t, sl_t) if (pl_t or sl_t) else None
        self._mark_auto_false_positives(match, license_rows)
        return {"copyright_rows": copyright_rows, "license_rows": license_rows, "text_cmp": text_cmp,
                "pl": pl, "sl": sl}

    # --- discrepancy keys ---------------------------------------------------
    def _discrepancies(self, cmp: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        rejected = {(d["field"], d["entry_key"]) for d in decisions if d["action"] in REJECTION_ACTIONS}
        out: dict[tuple[str, str], str] = {}
        for r in cmp["copyright_rows"]:
            if r.kind in ("sl_only", "variant", "pl_only") and (FIELD_COPYRIGHT, r.key) not in rejected:
                out[(FIELD_COPYRIGHT, r.key)] = r.kind
        for r in cmp["license_rows"]:
            if r.kind in ("sl_only", "pl_only", "spdx_diff") and not r.auto_false_positive                     and (FIELD_LICENSE_NAME, r.key) not in rejected:
                out[(FIELD_LICENSE_NAME, r.key)] = r.kind
        tc = cmp["text_cmp"]
        if tc and tc.recommendation == "replace_with_source" and tc.preferred:
            key = tc.preferred.content_hash or ""
            if (FIELD_LICENSE_TEXT, key) not in rejected:
                out[(FIELD_LICENSE_TEXT, key)] = "replace_with_source"
        elif tc and tc.recommendation == "no_source_text":
            if (FIELD_LICENSE_TEXT, "no_source_text") not in rejected:
                out[(FIELD_LICENSE_TEXT, "no_source_text")] = "no_source_text"
        return out

    def recompute(self, match_id: int, context: "RecomputeContext | None" = None) -> dict[str, Any]:
        match = context.match if context else self.match(match_id)
        decisions = context.decisions if context else _decision_rows(self.db, match_id)
        cmp = self.comparison(
            match,
            inventory=context.inventory if context else None,
            license_map=context.license_map if context else None,
            rules=context.rules if context else None,
        )
        discrepancies = self._discrepancies(cmp, decisions)
        meaningful = [d for d in decisions if d["action"] != NOTE_ONLY]
        decided = {(d["field"], d["entry_key"]) for d in meaningful}
        undecided = set(discrepancies) - decided
        state = match.get("review_state")
        if match["status"] in ("unmatched", "rejected"):
            status = "not_reviewed"
        elif state == "approved_for_sync":
            status = "approved_for_sync"
        elif state == "reviewed":
            status = "reviewed"
        elif not discrepancies and not meaningful:
            status = "not_reviewed"
        elif not meaningful:
            status = "review_required"
        elif undecided:
            status = "in_progress"
        else:
            status = "reviewed"
        self.db.execute(
            "UPDATE matches SET discrepancy_count=?, review_status=?, updated_at=? WHERE id=?",
            (len(discrepancies), status, now(), match_id),
        )
        return {"review_status": status, "discrepancy_count": len(discrepancies), "undecided": len(undecided)}

    # --- queue -------------------------------------------------------------
    def _side_maps(self, mapping_id: int, page: list[dict[str, Any]] | None = None
                   ) -> tuple[dict[tuple[str, str], list[str]], dict[tuple[str, str], list[str]]]:
        """License names and external identifiers per (side, external_id).

        With a page, only that page's components are read: a queue request for 50 rows must not
        pull the license rows and metadata blobs of a 10,000-component snapshot.
        """
        lic_names: dict[tuple[str, str], list[str]] = {}
        ident: dict[tuple[str, str], list[str]] = {}
        for side in ("pl", "sl"):
            snap = self.s.current_snapshot(mapping_id, side)
            if not snap:
                continue
            keys: list[str] | None = None
            if page is not None:
                keys = sorted({m[f"{side}_key"] for m in page if m[f"{side}_key"]})
                if not keys:
                    continue
            marks = ",".join("?" * len(keys)) if keys else ""
            params = (snap["id"], *keys) if keys else (snap["id"],)
            lic_scope = f" AND c.external_id IN ({marks})" if keys else ""
            comp_scope = f" AND external_id IN ({marks})" if keys else ""
            for r in self.db.query(
                "SELECT c.external_id AS k, l.name AS n FROM licenses l JOIN components c ON c.id=l.component_id"
                f" WHERE c.snapshot_id=? AND l.active=1{lic_scope}", params):
                lic_names.setdefault((side, r["k"]), []).append(r["n"])
            for r in self.db.query(
                f"SELECT external_id AS k, purl, metadata_json FROM components WHERE snapshot_id=?{comp_scope}",
                params):
                ids = [r["purl"] or ""]
                try:
                    meta = json.loads(r["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    meta = {}
                for key in ("external_ids", "externalIds", "inputExternalIds"):
                    val = meta.get(key)
                    if isinstance(val, list):
                        ids.extend(str(x) for x in val if isinstance(x, (str, int)))
                ident[(side, r["k"])] = [x for x in ids if x]
        return lic_names, ident

    @staticmethod
    def _haystack(match: dict[str, Any], lic_names: dict, ident: dict) -> str:
        """Everything a queue search may match on - one definition, used by search and by the row."""
        return " ".join(filter(None, [
            match["pl_name"], match["pl_version"], match["sl_name"], match["sl_version"], match["method"],
            match["pl_key"], match["sl_key"],
            *(lic_names.get(("pl", match["pl_key"]), [])), *(lic_names.get(("sl", match["sl_key"]), [])),
            *(ident.get(("pl", match["pl_key"]), [])), *(ident.get(("sl", match["sl_key"]), [])),
        ])).lower()

    def _queue_row(self, m: dict[str, Any], lic_names: dict, ident: dict) -> dict[str, Any]:
        return {
            "id": m["id"], "pl_name": m["pl_name"], "pl_version": m["pl_version"],
            "sl_name": m["sl_name"], "sl_version": m["sl_version"],
            "method": m["method"], "confidence": m["confidence"], "status": m["status"],
            "review_status": m["review_status"], "discrepancy_count": m["discrepancy_count"],
            "mismatch_reasons": json.loads(m.get("mismatch_json") or "[]"),
            "licenses": sorted(set(lic_names.get(("pl", m["pl_key"]), []) + lic_names.get(("sl", m["sl_key"]), []))),
        }

    @staticmethod
    def _passes(m: dict[str, Any], match_status: str, review_status: str, confidence: str) -> bool:
        if match_status and m["status"] != match_status:
            return False
        if review_status and m["review_status"] != review_status:
            return False
        if confidence == "high" and m["confidence"] < 0.9:
            return False
        if confidence == "low" and m["confidence"] >= 0.9:
            return False
        return True

    def list_components(self, mapping_id: int, *, q: str = "", review_status: str = "", match_status: str = "",
                        confidence: str = "", sort: str = "name", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        # Filter and sort on the match row alone; the expensive per-component maps are only built
        # for what is actually returned (or for everything, when a text search has to look at all).
        kept = [m for m in self.s._current_matches(mapping_id)
                if self._passes(m, match_status, review_status, confidence)]
        ql = q.strip().lower()
        lic_names: dict[tuple[str, str], list[str]] = {}
        ident: dict[tuple[str, str], list[str]] = {}
        if ql:
            lic_names, ident = self._side_maps(mapping_id)
            kept = [m for m in kept if ql in self._haystack(m, lic_names, ident)]
        pname = lambda r: (r["pl_name"] or "").lower()  # an SL-only row has no PL name
        key = {
            "name": lambda r: (pname(r), r["pl_version"] or ""),
            # triage order: the rows a reviewer must look at come first - most disagreements,
            # then least certain matches. Ties fall back to name so paging is stable.
            "confidence": lambda r: (r["confidence"], pname(r)),
            "discrepancies": lambda r: (-r["discrepancy_count"], pname(r)),
            "status": lambda r: (r["review_status"], pname(r)),
        }.get(sort, lambda r: (pname(r), r["pl_version"] or ""))
        kept.sort(key=key)
        total = len(kept)
        page = kept[offset: offset + limit]
        if not ql:
            lic_names, ident = self._side_maps(mapping_id, page)
        return {"total": total, "offset": offset, "limit": limit,
                "items": [self._queue_row(m, lic_names, ident) for m in page]}

    EXPORT_COLUMNS = ("pl_name", "pl_version", "sl_name", "sl_version", "match_status",
                      "review_status", "match_method", "confidence", "discrepancy_count",
                      "licenses", "mismatch_reasons")

    def export_csv(self, mapping_id: int) -> str:
        """Review report for a mapping: one CSV row per component, all pages."""
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=self.EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        offset, page = 0, self.list_components(mapping_id, offset=0, limit=200)
        while True:
            for item in page["items"]:
                writer.writerow({
                    "pl_name": item["pl_name"], "pl_version": item["pl_version"],
                    "sl_name": item["sl_name"], "sl_version": item["sl_version"],
                    "match_status": item["status"], "review_status": item["review_status"],
                    "match_method": item["method"], "confidence": item["confidence"],
                    "discrepancy_count": item["discrepancy_count"],
                    "licenses": "; ".join(item["licenses"]),
                    "mismatch_reasons": "; ".join(item["mismatch_reasons"]),
                })
            offset += page["limit"]
            if offset >= page["total"]:
                return out.getvalue()
            page = self.list_components(mapping_id, offset=offset, limit=200)

    def list_false_positive_rules(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM false_positive_rules ORDER BY id")

    def revoke_false_positive_rule(self, rule_id: int, reviewer: str = "reviewer") -> dict[str, Any]:
        row = self.db.one("SELECT * FROM false_positive_rules WHERE id=?", (rule_id,))
        if not row:
            raise KeyError(f"false-positive rule {rule_id} not found")
        with self.db.transaction():
            self.db.execute("UPDATE false_positive_rules SET revoked_at=? WHERE id=?", (now(), rule_id))
            self.db.audit(reviewer, "false_positive.revoked", "false_positive_rule", rule_id,
                          {"license": row["license_name"], "scope": row["scope"]}, now())
        return {"ok": True, "revoked": rule_id}

    def export_html(self, mapping_id: int) -> str:
        """Review report for one mapping as a self-contained HTML table."""
        rows = []
        offset, page = 0, self.list_components(mapping_id, offset=0, limit=200)
        while True:
            rows.extend(page["items"])
            offset += page["limit"]
            if offset >= page["total"]:
                break
            page = self.list_components(mapping_id, offset=offset, limit=200)
        mapping = self.s.mapping(mapping_id)

        def value(row: dict[str, Any], col: str) -> str:
            raw = row.get(col)
            if isinstance(raw, list):
                return "; ".join(str(x) for x in raw)
            return str(raw or "")

        body = "\n".join(
            "<tr>" + "".join("<td>" + html.escape(value(row, col)) + "</td>" for col in
                             ("pl_name", "pl_version", "sl_name", "sl_version", "status",
                              "review_status", "method", "confidence", "discrepancy_count",
                              "licenses", "mismatch_reasons")) + "</tr>"
            for row in rows
        )
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>Review report — {html.escape(mapping['name'])}</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:2rem}table{border-collapse:collapse;width:100%}"
            "th,td{border:1px solid #ccc;padding:.4rem;text-align:left;font-size:.9rem}"
            "th{background:#f2f4f8}</style></head><body>"
            f"<h1>{html.escape(mapping['name'])}</h1>"
            f"<p>PL {html.escape(mapping['pl_project'])} {html.escape(mapping['pl_version'] or '')} · "
            f"SL {html.escape(mapping['sl_scan'])} · {len(rows)} components</p>"
            "<table><thead><tr>" + "".join(
                "<th>" + html.escape(col.replace('_', ' ')) + "</th>" for col in
                ("pl_name", "pl_version", "sl_name", "sl_version", "status", "review_status",
                 "method", "confidence", "discrepancy_count", "licenses", "mismatch_reasons")) +
            "</tr></thead><tbody>" + body + "</tbody></table></body></html>"
        )

    def _previous_mapping(self, mapping_id: int) -> dict[str, Any] | None:
        current = self.s.mapping(mapping_id)
        return self.db.one(
            "SELECT * FROM mappings WHERE pl_project=? AND pl_connection_id=? AND created_at<? "
            "AND pl_version IS NOT NULL AND pl_version != ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (current["pl_project"], current["pl_connection_id"], current["created_at"], current["pl_version"]),
        )

    def delta(self, mapping_id: int, apply_decisions: bool = False) -> dict[str, Any]:
        current = self.s.mapping(mapping_id)
        previous = self._previous_mapping(mapping_id)
        cur_pl_snap = self.s.current_snapshot(mapping_id, "pl")
        prev_pl_snap = self.s.current_snapshot(previous["id"], "pl") if previous else None
        cur_sl_snap = self.s.current_snapshot(mapping_id, "sl")
        prev_sl_snap = self.s.current_snapshot(previous["id"], "sl") if previous else None

        cur_pl_comps = self.s.snapshot_components(cur_pl_snap["id"]) if cur_pl_snap else []
        prev_pl_comps = self.s.snapshot_components(prev_pl_snap["id"]) if prev_pl_snap else []
        prev_by_key = {c["external_id"]: c for c in prev_pl_comps}
        new = [c for c in cur_pl_comps if c["external_id"] not in prev_by_key]
        changed = [c for c in cur_pl_comps if c["external_id"] in prev_by_key
                   and c["source_version"] != prev_by_key[c["external_id"]]["source_version"]]
        unchanged = [c for c in cur_pl_comps if c["external_id"] in prev_by_key and c not in changed]

        cur_matches = self.s._current_matches(mapping_id)
        prev_matches = self.s._current_matches(previous["id"]) if previous else []
        cur_by_pl = {m["pl_key"]: m for m in cur_matches if m["pl_key"]}
        prev_by_pl = {m["pl_key"]: m for m in prev_matches if m["pl_key"]}
        cur_sl_comps = self.s.snapshot_components(cur_sl_snap["id"]) if cur_sl_snap else []
        prev_sl_comps = self.s.snapshot_components(prev_sl_snap["id"]) if prev_sl_snap else []
        cur_sl_by_key = {c["external_id"]: c for c in cur_sl_comps}
        prev_sl_by_key = {c["external_id"]: c for c in prev_sl_comps}

        # A matched component changes when either side's observation hash moves.
        changed_keys = {c["external_id"] for c in changed}
        for m in cur_matches:
            pm = prev_by_pl.get(m["pl_key"])
            if not pm or not m["sl_key"] or not pm["sl_key"]:
                continue
            cur_sl = cur_sl_by_key.get(m["sl_key"])
            old_sl = prev_sl_by_key.get(pm["sl_key"])
            if cur_sl and old_sl and cur_sl["source_version"] != old_sl["source_version"]:
                component = next((c for c in cur_pl_comps if c["external_id"] == m["pl_key"]), None)
                if component and component["external_id"] not in changed_keys:
                    changed.append(component)
                    changed_keys.add(component["external_id"])

        findings = []
        if cur_pl_snap and prev_pl_snap:
            cur_obs_pl = self.s.components_observations(cur_pl_snap["id"])
            prev_obs_pl = self.s.components_observations(prev_pl_snap["id"])
            cur_obs_sl = self.s.components_observations(cur_sl_snap["id"]) if cur_sl_snap else {}
            prev_obs_sl = self.s.components_observations(prev_sl_snap["id"]) if prev_sl_snap else {}
            for c in changed + new:
                prior = prev_by_key.get(c["external_id"])
                cur_pl = cur_obs_pl.get(c["id"], {"licenses": [], "copyrights": []})
                old_pl = prev_obs_pl.get(prior["id"], {"licenses": [], "copyrights": []}) if prior else {"licenses": [], "copyrights": []}
                cur_match = cur_by_pl.get(c["external_id"])
                old_match = prev_by_pl.get(c["external_id"])
                cur_sl = {}
                if cur_match and cur_match["sl_key"]:
                    cur_sl_comp = cur_sl_by_key.get(cur_match["sl_key"])
                    if cur_sl_comp:
                        cur_sl = cur_obs_sl.get(cur_sl_comp["id"], {"licenses": [], "copyrights": []})
                old_sl = {}
                if old_match and old_match["sl_key"]:
                    old_sl_comp = prev_sl_by_key.get(old_match["sl_key"])
                    if old_sl_comp:
                        old_sl = prev_obs_sl.get(old_sl_comp["id"], {"licenses": [], "copyrights": []})
                new_licenses = sorted(
                    ({norm_license(x["name"]) for x in cur_pl["licenses"]}
                     | {norm_license(x["name"]) for x in cur_sl["licenses"]})
                    - {norm_license(x["name"]) for x in old_pl["licenses"]}
                    - {norm_license(x["name"]) for x in old_sl["licenses"]}
                )
                new_copyrights = sorted(
                    ({copyright_key(x["text"]) for x in cur_pl["copyrights"]}
                     | {copyright_key(x["text"]) for x in cur_sl["copyrights"]})
                    - {copyright_key(x["text"]) for x in old_pl["copyrights"]}
                    - {copyright_key(x["text"]) for x in old_sl["copyrights"]}
                )
                if new_licenses or new_copyrights:
                    findings.append({"component": c["name"], "version": c["version"],
                                     "new_licenses": new_licenses, "new_copyrights": new_copyrights})

        carried = []
        if apply_decisions and previous:
            cur_matches = self.s._current_matches(mapping_id)
            prev_matches = self.s._current_matches(previous["id"])
            prev_by_pl = {m["pl_key"]: m for m in prev_matches if m["pl_key"]}
            with self.db.transaction():
                for m in cur_matches:
                    pm = prev_by_pl.get(m["pl_key"])
                    if not pm:
                        continue
                    for d in _decision_rows(self.db, pm["id"]):
                        exists = self.db.one(
                            "SELECT id FROM decisions WHERE match_id=? AND field=? AND entry_key=?",
                            (m["id"], d["field"], d["entry_key"]),
                        )
                        if exists:
                            continue
                        self.db.execute(
                            "INSERT INTO decisions(mapping_id,match_id,field,entry_key,action,approved_value,"
                            "reviewer,note,source_version,pl_source_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (mapping_id, m["id"], d["field"], d["entry_key"], d["action"],
                             d["approved_value"], d["reviewer"], d["note"], d["source_version"],
                             d["pl_source_version"], now()),
                        )
                        self.db.audit("delta", "decision.carried", "match", m["id"],
                                      {"from_match": pm["id"], "field": d["field"]}, now())
                        carried.append({"match_id": m["id"], "field": d["field"], "entry_key": d["entry_key"]})
                for m in cur_matches:
                    self.recompute(m["id"])
        return {
            "mapping_id": mapping_id,
            "current": {"project": current["pl_project"], "version": current["pl_version"]},
            "previous": {"mapping_id": previous["id"], "version": previous["pl_version"]} if previous else None,
            "new_components": [{"name": c["name"], "version": c["version"]} for c in new],
            "changed_components": [{"name": c["name"], "version": c["version"]} for c in changed],
            "unchanged": len(unchanged),
            "new_findings": findings,
            "carried_decisions": carried,
        }
    # --- workspace ---------------------------------------------------------
    def _decision_view(self, cmp: dict[str, Any], decided: dict[tuple[str, str], dict[str, Any]],
                       rejected: dict[tuple[str, str], dict[str, Any]]) -> tuple[list, list, dict | None]:
        """The three comparison tables with each row's decision attached."""
        copyright_rows = []
        for r in cmp["copyright_rows"]:
            copyright_rows.append({**asdict(r), "decision": decided.get((FIELD_COPYRIGHT, r.key)),
                                   "rejected": bool(rejected.get((FIELD_COPYRIGHT, r.key)))})
        license_rows = []
        for r in cmp["license_rows"]:
            license_rows.append({**asdict(r), "decision": decided.get((FIELD_LICENSE_NAME, r.key)),
                                 "rejected": bool(rejected.get((FIELD_LICENSE_NAME, r.key)))})

        text_view = None
        tc = cmp["text_cmp"]
        if tc:
            text_key = tc.preferred.content_hash if tc.preferred else "no_source_text"
            text_view = {
                "pl_current": asdict(tc.pl_current) if tc.pl_current else None,
                "candidates": [asdict(c) for c in tc.candidates],
                "preferred": asdict(tc.preferred) if tc.preferred else None,
                "recommendation": tc.recommendation,
                "reasons": tc.reasons,
                "decision": decided.get((FIELD_LICENSE_TEXT, text_key)),
                "rejected": bool(rejected.get((FIELD_LICENSE_TEXT, text_key))),
            }
        return copyright_rows, license_rows, text_view

    def _detail_warnings(self, match: dict[str, Any], cmp: dict[str, Any],
                         pl_comp: dict[str, Any] | None, sl_comp: dict[str, Any] | None) -> list[str]:
        warnings: list[str] = []
        if pl_comp and sl_comp:
            for side, comp in (("PL", pl_comp), ("SL", sl_comp)):
                if not comp["source_version"]:
                    warnings.append(f"{side} component has no observation hash")
        if match["status"] == "suggested":
            warnings.append("fuzzy match requires confirmation")
        if match["status"] == "unmatched":
            warnings.append("component is unmatched; confirm a manual match before reviewing")
        if cmp["text_cmp"] and cmp["text_cmp"].recommendation == "no_source_text":
            warnings.append("no identified source-file license text; component flagged for further review")
        if not pl_comp or not sl_comp:
            warnings.append("one side missing from the current snapshot")
        return warnings

    def _reassignment_candidates(self, match: dict[str, Any]) -> list[dict[str, Any]]:
        """Unmatched SL components ranked by name similarity - what a reassignment may pick."""
        candidates = []
        snap = self.s.current_snapshot(match["mapping_id"], "sl")
        matched_sl = {m["sl_key"] for m in self.s._current_matches(match["mapping_id"]) if m["sl_key"]}
        if snap:
            for c in self.s.snapshot_components(snap["id"]):
                if c["external_id"] in matched_sl:
                    continue
                ratio = SequenceMatcher(None, norm_name(match["pl_name"]), norm_name(c["name"])).ratio()
                candidates.append({"sl_key": c["external_id"], "sl_name": c["name"], "sl_version": c["version"],
                                   "similarity": round(ratio, 2)})
            candidates.sort(key=lambda x: -x["similarity"])
        return candidates

    def detail(self, match_id: int) -> dict[str, Any]:
        match = self.match(match_id)
        cmp = self.comparison(match)
        decisions = _decision_rows(self.db, match_id)
        notes = self.db.query("SELECT * FROM notes WHERE match_id=? ORDER BY id", (match_id,))
        rejected = {(d["field"], d["entry_key"]): d for d in decisions if d["action"] in REJECTION_ACTIONS}
        decided = {(d["field"], d["entry_key"]): d for d in decisions}
        copyright_rows, license_rows, text_view = self._decision_view(cmp, decided, rejected)
        pl_comp = cmp["pl"]["component"] if cmp["pl"] else None
        sl_comp = cmp["sl"]["component"] if cmp["sl"] else None

        warnings = self._detail_warnings(match, cmp, pl_comp, sl_comp)
        stale = self._stale_flags(match, decisions, pl_comp, sl_comp)
        from .sync import PreviewContext, preview_match  # local import keeps the module graph acyclic
        preview = []
        if pl_comp and sl_comp:
            # the rows this view already read answer the preview's questions; no reads of its own
            sides = {"pl": {match["pl_key"]: cmp["pl"]}, "sl": {match["sl_key"]: cmp["sl"]}}
            preview = preview_match(self.s, match, decisions,
                                    PreviewContext(self.s, match["mapping_id"], sides=sides))

        candidates = []
        if match["status"] == "unmatched" or match["status"] == "rejected":
            candidates = self._reassignment_candidates(match)

        return {
            "match": match,
            "pl": cmp["pl"], "sl": cmp["sl"],
            "comparison": {"copyrights": copyright_rows, "licenses": license_rows, "license_text": text_view},
            "decisions": decisions, "notes": notes, "suggestions": self.suggestions(match_id),
            "review_status": match["review_status"], "discrepancy_count": match["discrepancy_count"],
            "sync_preview": preview, "stale": stale, "warnings": warnings, "candidates": candidates,
            "false_positive_rules": self._active_rules(match),
        }

    def _stale_flags(self, match: dict[str, Any], decisions: list[dict[str, Any]],
                     pl_comp: dict[str, Any] | None, sl_comp: dict[str, Any] | None) -> list[dict[str, Any]]:
        out = []
        for d in decisions:
            if d["action"] not in (APPROVE_SL, CORRECT):
                continue
            current = (sl_comp or {}).get("source_version", "")
            if d["source_version"] and current and d["source_version"] != current:
                out.append({"field": d["field"], "entry_key": d["entry_key"],
                            "reason": "SL observations changed after this decision"})
            current_pl = (pl_comp or {}).get("source_version", "")
            if d["pl_source_version"] and current_pl and d["pl_source_version"] != current_pl:
                out.append({"field": d["field"], "entry_key": d["entry_key"],
                            "reason": "PL values changed after this decision"})
        return out

    # --- decisions ---------------------------------------------------------
    def _sl_source_version(self, match: dict[str, Any]) -> str:
        row = self.s.component_by_key(match["mapping_id"], "sl", match["sl_key"]) if match["sl_key"] else None
        return (row or {}).get("source_version", "")

    def _pl_source_version(self, match: dict[str, Any]) -> str:
        row = self.s.component_by_key(match["mapping_id"], "pl", match["pl_key"]) if match["pl_key"] else None
        return (row or {}).get("source_version", "")

    def _copyright_value(self, cmp: dict[str, Any], key: str, action: str, value: str) -> str:
        pl_texts = {copyright_key(r["text"]): r["text"] for r in (cmp["pl"] or {}).get("copyrights", [])}
        sl_texts = {copyright_key(r["text"]): r["text"] for r in (cmp["sl"] or {}).get("copyrights", [])}
        rows = {r.key: r for r in cmp["copyright_rows"]}
        if action == APPROVE_SL:
            if key in sl_texts:
                return sl_texts[key]
            if rows.get(key) is not None and rows[key].sl_text:
                return rows[key].sl_text
            raise ValueError("approve_sl requires an SL copyright entry key")
        if action == ACCEPT_PL:
            if key in pl_texts:
                return pl_texts[key]
            if rows.get(key) is not None and rows[key].pl_text:
                return rows[key].pl_text
            raise ValueError("accept_pl requires a PL copyright entry key")
        if action == CORRECT:
            if not value:
                raise ValueError("correct requires approved_value")
            return value
        if key and key not in sl_texts and key not in pl_texts and key not in rows:
            raise ValueError("entry_key does not exist in either side")
        return value

    def _license_value(self, cmp: dict[str, Any], key: str, action: str, value: str) -> str:
        lookup = self.s.license_map_lookup()
        def canonical(name: str) -> str:
            row = lookup.get(norm_license(name))
            return norm_license(row["bd_license"] if row and row.get("bd_license") else name)
        pl_names = {canonical(r["name"]): r["name"] for r in (cmp["pl"] or {}).get("licenses", [])}
        sl_names = {canonical(r["name"]): r["name"] for r in (cmp["sl"] or {}).get("licenses", [])}
        if action == APPROVE_SL:
            if key not in sl_names:
                raise ValueError("approve_sl requires an SL license entry key")
            name = sl_names[key]
            mapped = lookup.get(norm_license(name))
            return mapped["bd_license"] if mapped and mapped.get("bd_license") else name
        if action == ACCEPT_PL:
            if key not in pl_names:
                raise ValueError("accept_pl requires a PL license entry key")
            return pl_names[key]
        if action == CORRECT and not value:
            raise ValueError("correct requires approved_value")
        if action not in (APPROVE_SL, ACCEPT_PL, CORRECT) and key not in sl_names and key not in pl_names:
            raise ValueError("entry_key does not exist in either side")
        return value

    def _license_text_value(self, cmp: dict[str, Any], key: str, action: str, value: str) -> str:
        # Never invent text: an approved text is one of the identified source-file candidates.
        tc = cmp["text_cmp"]
        sources = {c.content_hash: c.text for c in (tc.candidates if tc else [])
                   if c.side == "sl" and c.kind == KIND_SOURCE}
        if action in (APPROVE_SL, CORRECT):
            if key not in sources:
                raise ValueError("license text must come from an identified SL source-file candidate")
            return sources[key]
        return value

    def decide(self, match_id: int, field: str, entry_key: str, action: str, *,
               approved_value: str | None = None, note: str = "", reviewer: str = "reviewer",
               scope: str = "component", path_pattern: str = "*") -> dict[str, Any]:
        if field not in FIELDS:
            raise ValueError(f"unknown field {field!r}")
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        match = self.match(match_id)
        cmp = self.comparison(match)
        key = entry_key or ""
        value = (approved_value or "").strip()

        if field == FIELD_COPYRIGHT:
            value = self._copyright_value(cmp, key, action, value)
        elif field == FIELD_LICENSE_NAME:
            value = self._license_value(cmp, key, action, value)
        else:  # license text
            value = self._license_text_value(cmp, key, action, value)

        if action == NOTE_ONLY and not note:
            raise ValueError("note_only requires a note")

        src = self._sl_source_version(match) if action in (APPROVE_SL, CORRECT) else self._pl_source_version(match)
        pl_src = self._pl_source_version(match)
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO decisions(mapping_id,match_id,field,entry_key,action,approved_value,reviewer,note,"
                "source_version,pl_source_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(match_id,field,entry_key) DO UPDATE SET action=excluded.action,"
                " approved_value=excluded.approved_value, reviewer=excluded.reviewer, note=excluded.note,"
                " source_version=excluded.source_version, pl_source_version=excluded.pl_source_version,"
                " created_at=excluded.created_at",
                (match["mapping_id"], match_id, field, key, action, value or None, reviewer, note, src, pl_src, now()),
            )
            if action == FALSE_POSITIVE and field == FIELD_LICENSE_NAME:
                if scope not in ("component", "version", "global"):
                    raise ValueError("scope must be component, version, or global")
                rule_match = match_id if scope == "component" else None
                rule_mapping = match["mapping_id"] if scope == "version" else None
                cur = self.db.execute(
                    "INSERT INTO false_positive_rules(license_name,path_pattern,scope,match_id,mapping_id,"
                    "created_by,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (norm_license(key), path_pattern or "*", scope, rule_match, rule_mapping,
                     reviewer, note, now()),
                )
                self.db.audit(reviewer, "false_positive.rule", "false_positive_rule", cur.lastrowid,
                              {"license": key, "scope": scope, "path_pattern": path_pattern}, now())
            self.db.audit(reviewer, f"decision.{action}", "match", match_id,
                          {"field": field, "entry_key": key, "approved_value": value}, now())
            result = self.recompute(match_id)
            decision = self.db.one("SELECT * FROM decisions WHERE match_id=? AND field=? AND entry_key=?",
                                   (match_id, field, key))
        return {"ok": True, **result, "decision": decision}

    # --- machine suggestions (future LLM assistance; never auto-approved) --
    def add_suggestion(self, match_id: int, field: str, entry_key: str, value: str, *,
                       confidence: float = 0.0, model: str = "", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        if field not in FIELDS:
            raise ValueError(f"unknown field {field!r}")
        match = self.match(match_id)
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO suggestions(mapping_id,match_id,field,entry_key,value,confidence,model,evidence_json,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (match["mapping_id"], match_id, field, entry_key, value, float(confidence), model,
                 json.dumps(evidence or {}), now()),
            )
            self.db.audit("machine", "suggestion.recorded", "suggestion", cur.lastrowid,
                          {"field": field, "model": model, "confidence": confidence}, now())
        return {"ok": True, "id": cur.lastrowid}

    def suggestions(self, match_id: int) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM suggestions WHERE match_id=? ORDER BY id", (match_id,))
        for r in rows:
            r["evidence"] = json.loads(r.pop("evidence_json") or "{}")
        return rows

    def decide_suggestion(self, suggestion_id: int, action: str, reviewer: str = "reviewer",
                          note: str = "") -> dict[str, Any]:
        row = self.db.one("SELECT * FROM suggestions WHERE id=?", (suggestion_id,))
        if not row:
            raise KeyError(f"suggestion {suggestion_id} not found")
        if action not in ("accept", "reject"):
            raise ValueError("suggestion decision must be accept or reject")
        if action == "reject":
            with self.db.transaction():
                self.db.execute("UPDATE suggestions SET status='rejected', decided_by=?, decided_at=? WHERE id=?",
                                (reviewer, now(), suggestion_id))
                self.db.audit(reviewer, "suggestion.rejected", "suggestion", suggestion_id,
                              {"model": row["model"]}, now())
            return {"ok": True, "status": "rejected"}
        # acceptance always becomes a human decision, validated like any other decision
        with self.db.transaction():
            result = self.decide(row["match_id"], row["field"], row["entry_key"], APPROVE_SL,
                                 approved_value=row["value"], note=note, reviewer=reviewer)
            self.db.execute("UPDATE suggestions SET status='accepted', decided_by=?, decided_at=? WHERE id=?",
                            (reviewer, now(), suggestion_id))
            self.db.audit(reviewer, "suggestion.accepted", "suggestion", suggestion_id,
                          {"model": row["model"], "field": row["field"]}, now())
        return {"ok": True, "status": "accepted", **result}

    def add_note(self, match_id: int, body: str, reviewer: str = "reviewer") -> dict[str, Any]:
        match = self.match(match_id)
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO notes(mapping_id,match_id,reviewer,body,created_at) VALUES(?,?,?,?,?)",
                (match["mapping_id"], match_id, reviewer, body, now()),
            )
            self.db.audit(reviewer, "note.added", "match", match_id, {"note_id": cur.lastrowid}, now())
        return {"ok": True, "id": cur.lastrowid}

    def set_review_state(self, match_id: int, state: str | None, reviewer: str = "reviewer") -> dict[str, Any]:
        if state not in (None, "in_progress", "reviewed", "approved_for_sync"):
            raise ValueError("invalid review state")
        match = self.match(match_id)
        decisions = _decision_rows(self.db, match_id)
        cmp = self.comparison(match)
        undecided = set(self._discrepancies(cmp, decisions)) - {(d["field"], d["entry_key"]) for d in decisions if d["action"] != NOTE_ONLY}
        if state == "approved_for_sync":
            if undecided:
                raise ValueError(f"{len(undecided)} discrepant field(s) still undecided")
            if not any(d["action"] in (APPROVE_SL, CORRECT) for d in decisions):
                raise ValueError("no approved SL-derived correction to synchronize")
        with self.db.transaction():
            self.db.execute("UPDATE matches SET review_state=?, updated_at=? WHERE id=?", (state, now(), match_id))
            self.db.audit(reviewer, f"review.{state or 'reset'}", "match", match_id, {"state": state}, now())
            result = self.recompute(match_id)
        return {"ok": True, **result}

    # --- match management --------------------------------------------------
    def confirm_match(self, match_id: int, reviewer: str = "reviewer") -> dict[str, Any]:
        match = self.match(match_id)
        if not match["pl_key"] or not match["sl_key"]:
            raise ValueError("both sides must be assigned before confirmation")
        with self.db.transaction():
            self.db.execute("UPDATE matches SET status='confirmed', confirmed_by=?, confirmed_at=?, updated_at=?"
                            " WHERE id=?", (reviewer, now(), now(), match_id))
            self.db.audit(reviewer, "match.confirmed", "match", match_id,
                          {"method": match["method"], "confidence": match["confidence"]}, now())
            result = self.recompute(match_id)
        return {"ok": True, **result}

    def reassign(self, match_id: int, *, sl_key: str | None = None, pl_key: str | None = None,
                 reviewer: str = "reviewer") -> dict[str, Any]:
        match = self.match(match_id)
        mid = match["mapping_id"]
        new_pl = pl_key if pl_key is not None else match["pl_key"]
        new_sl = sl_key if sl_key is not None else match["sl_key"]
        if new_pl and not self.s.component_by_key(mid, "pl", new_pl):
            raise ValueError(f"PL component {new_pl!r} not in current snapshot")
        if new_sl and not self.s.component_by_key(mid, "sl", new_sl):
            raise ValueError(f"SL component {new_sl!r} not in current snapshot")
        pl_c = self.s.component_by_key(mid, "pl", new_pl) if new_pl else None
        sl_c = self.s.component_by_key(mid, "sl", new_sl) if new_sl else None
        with self.db.transaction():
            self.db.execute(
                "UPDATE matches SET pl_key=?, sl_key=?, pl_name=?, pl_version=?, sl_name=?, sl_version=?,"
                " method='manual', confidence=1.0, status='confirmed', rationale_json=?, mismatch_json=?,"
                " confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?",
                (new_pl, new_sl, (pl_c or {}).get("name", ""), (pl_c or {}).get("version"),
                 (sl_c or {}).get("name", ""), (sl_c or {}).get("version"),
                 json.dumps(["manual assignment by reviewer"]),
                 json.dumps([f"PL {((pl_c or {}).get('name'))} {((pl_c or {}).get('version'))} vs SL "
                             f"{((sl_c or {}).get('name'))} {((sl_c or {}).get('version'))}"]),
                 reviewer, now(), now(), match_id),
            )
            self.db.audit(reviewer, "match.reassigned", "match", match_id,
                          {"pl_key": new_pl, "sl_key": new_sl}, now())
            result = self.recompute(match_id)
        return {"ok": True, **result}

    def reject_match(self, match_id: int, reason: str = "", reviewer: str = "reviewer") -> dict[str, Any]:
        with self.db.transaction():
            self.db.execute("UPDATE matches SET status='rejected', updated_at=? WHERE id=?", (now(), match_id))
            self.db.audit(reviewer, "match.rejected", "match", match_id, {"reason": reason}, now())
            result = self.recompute(match_id)
        return {"ok": True, **result}
