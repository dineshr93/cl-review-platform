"""Review workflow: queue, component workspace, field-level decisions, notes, statuses."""
from __future__ import annotations

import csv
import html
import io
import json
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from typing import Any

from .comparison import (compare_copyrights, compare_license_texts_by_license,
                         compare_licenses, split_text_key)
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
    KIND_GENERIC,
    KIND_SOURCE,
    NOTE_ONLY,
    REJECTION_ACTIONS,
    LicenseTextCandidate,
    copyright_key,
    name_key,
    name_similarity,
    norm_license,
    pair_by_name,
    query_matches,
    sha256,
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
        file_licenses: dict[str, list[str]] = {}
        for r in (sl or {}).get("licenses", []):
            for path in json.loads(r.get("evidence_paths_json") or "[]"):
                file_licenses.setdefault(path, []).append(r["name"])
        loaded = self._loaded_files(match, sl, inventory)
        for f in loaded:
            sl_t.append(LicenseTextCandidate(kind=KIND_SOURCE, text=f["text"], provenance=f["provenance"],
                                             file_name=f["file_path"].rsplit("/", 1)[-1], file_path=f["file_path"],
                                             content_hash=f["content_hash"], is_license_file=True))
            file_licenses.setdefault(f["file_path"], []).extend(json.loads(f["licenses_json"] or "[]"))
        copyright_rows = compare_copyrights(pl_c, sl_c) if (pl_c or sl_c) else []
        lookup = license_map if license_map is not None else self.s.license_map_lookup()
        license_rows = compare_licenses(pl_l, sl_l, lookup) if (pl_l or sl_l) else []
        text_rows = (compare_license_texts_by_license(pl_t, sl_t, pl_l, file_licenses, lookup)
                     if (pl_t or sl_t) else [])
        self._mark_auto_false_positives(match, license_rows, rules)
        return {"copyright_rows": copyright_rows, "license_rows": license_rows, "text_rows": text_rows,
                "text_cmp": text_rows[0].cmp if text_rows else None, "loaded_files": loaded,
                "pl": pl, "sl": sl}

    def _loaded_files(self, match: dict[str, Any], sl: dict[str, Any] | None,
                      inventory: dict | None = None) -> list[dict[str, Any]]:
        """License files loaded on demand for this SL component, while its observations still match.

        A refresh hands them in with the inventory (``inventory["loaded"]``, one read for the
        mapping); a single-component read asks for its own.
        """
        if not sl or not match["sl_key"]:
            return []
        version = sl["component"]["source_version"]
        if inventory is not None and "loaded" in inventory:
            return [f for f in inventory["loaded"].get(match["sl_key"], []) if f["sl_source_version"] == version]
        return self.db.query(
            "SELECT * FROM sl_license_files WHERE mapping_id=? AND sl_key=? AND sl_source_version=? ORDER BY file_path",
            (match["mapping_id"], match["sl_key"], version))

    @staticmethod
    def _text_row(rows: list[Any], key: str) -> Any:
        """The license-text row a decision key belongs to: by its ``@license``, else by the text it names."""
        if not rows:
            return None
        base, lkey = split_text_key(key)
        for row in rows:
            if lkey and row.lkey == lkey:
                return row
        if not lkey:
            for row in rows:
                if any(c.content_hash == base for c in row.cmp.candidates + row.unassigned):
                    return row
            return rows[0]
        return None

    def _decided_keys(self, cmp: dict[str, Any], decisions: list[dict[str, Any]]) -> set[tuple[str, str]]:
        """Decided (field, key) pairs; any license-text decision settles its license's row."""
        out = {(d["field"], d["entry_key"]) for d in decisions if d["action"] != NOTE_ONLY}
        for d in decisions:
            if d["field"] == FIELD_LICENSE_TEXT and d["action"] != NOTE_ONLY:
                row = self._text_row(cmp.get("text_rows") or [], d["entry_key"])
                if row is not None:
                    out.add((FIELD_LICENSE_TEXT, row.discrepancy_key))
        return out

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
        rejected_rows = {self._text_row(cmp.get("text_rows") or [], k).discrepancy_key
                         for f, k in rejected if f == FIELD_LICENSE_TEXT and self._text_row(cmp.get("text_rows") or [], k)}
        for row in cmp.get("text_rows") or []:
            tc = row.cmp
            if tc.recommendation in ("replace_with_source", "no_source_text") and row.discrepancy_key not in rejected_rows:
                out[(FIELD_LICENSE_TEXT, row.discrepancy_key)] = tc.recommendation
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
        undecided = set(discrepancies) - self._decided_keys(cmp, decisions)
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

    def _query_hit(self, ql: str, m: dict[str, Any], lic_names: dict, ident: dict) -> bool:
        haystack = self._haystack(m, lic_names, ident)
        return (query_matches(ql, m["pl_name"], m["pl_version"], haystack)
                or query_matches(ql, m["sl_name"], m["sl_version"], haystack))

    def _queue_row(self, m: dict[str, Any], lic_names: dict, ident: dict) -> dict[str, Any]:
        return {
            "id": m["id"], "pl_key": m["pl_key"], "sl_key": m["sl_key"],
            "pl_name": m["pl_name"], "pl_version": m["pl_version"],
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
            kept = [m for m in kept if self._query_hit(ql, m, lic_names, ident)]
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

    def export_csv(self, mapping_id: int, ids: set[int] | None = None) -> str:
        """Review report for a mapping: one CSV row per component, all pages (or just ``ids``)."""
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=self.EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        offset, page = 0, self.list_components(mapping_id, offset=0, limit=200)
        while True:
            for item in page["items"]:
                if ids is not None and item["id"] not in ids:
                    continue
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

    def load_sl_license_files(self, match_id: int, reviewer: str = "reviewer") -> dict[str, Any]:
        """Read this SL component's license files from the scanner now, for review.

        One upload's per-file listing and its license-file texts: seconds, where reading every
        upload's files at refresh time would cost the whole folder. Kept beside the snapshot
        (``sl_license_files``) and replaced on the next load.
        """
        match = self.match(match_id)
        if not match["sl_key"]:
            raise ValueError("this component has no SL side to read license files from")
        sl = self.s.component_by_key(match["mapping_id"], "sl", match["sl_key"])
        if not sl:
            raise ValueError("the SL component is not in the current snapshot")
        mapping = self.s.mapping(match["mapping_id"])
        conn = self.s.connector_for(mapping["sl_connection_id"])
        reader = getattr(conn, "license_files", None)
        if reader is None:
            raise ValueError(f"{conn.tool} cannot list the license files of one component")
        meta = json.loads(sl.get("metadata_json") or "{}")
        upload = str(meta.get("upload_id") or match["sl_key"])
        files = reader(upload)   # network: outside the transaction
        with self.db.transaction():
            self.db.execute("DELETE FROM sl_license_files WHERE mapping_id=? AND sl_key=?",
                            (match["mapping_id"], match["sl_key"]))
            for f in files:
                self.db.execute(
                    "INSERT INTO sl_license_files(mapping_id,sl_key,sl_source_version,file_path,text,content_hash,"
                    "licenses_json,provenance,loaded_by,loaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (match["mapping_id"], match["sl_key"], sl["source_version"], f["file_path"], f["text"],
                     f.get("content_hash") or sha256(f["text"]), json.dumps(f.get("licenses") or []),
                     f.get("provenance") or f"{conn.tool} license file, loaded on demand",
                     reviewer, now()))
            self.db.audit(reviewer, "sl_license_files.loaded", "match", match_id,
                          {"upload": upload, "files": len(files)}, now())
            result = self.recompute(match_id)
        return {"files": len(files), **result}

    # --- bulk actions --------------------------------------------------------
    BULK_ACTIONS = ("confirm", "approve", "unlink")

    def resolve_selection(self, mapping_id: int, ids: Any = None, filters: dict[str, Any] | None = None) -> list[int]:
        """The selected match ids: an explicit list, or every row the queue filters show (``filters``)."""
        if filters is not None:
            f = filters or {}
            return [i["id"] for i in self.list_components(
                mapping_id, q=str(f.get("q") or ""), review_status=str(f.get("review_status") or ""),
                match_status=str(f.get("status") or ""), confidence=str(f.get("confidence") or ""),
                offset=0, limit=10 ** 9)["items"]]
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ValueError("ids must be a list of component ids")
        own = {m["id"] for m in self.db.query("SELECT id FROM matches WHERE mapping_id=?", (mapping_id,))}
        foreign = [i for i in ids if i not in own]
        if foreign:
            raise ValueError(f"component(s) {foreign[:5]} do not belong to this mapping")
        return list(dict.fromkeys(ids))

    def bulk(self, mapping_id: int, action: str, ids: list[int], reviewer: str = "reviewer") -> dict[str, Any]:
        """Apply one action to many components, each in its own transaction.

        A component the action does not fit (an unmatched row cannot be confirmed) is reported in
        ``failed`` with the same reason a single click would give, and the others still go through:
        bulk work is many ordinary decisions, each audited under the reviewer's name.
        """
        if action not in self.BULK_ACTIONS:
            raise ValueError(f"bulk action must be one of {', '.join(self.BULK_ACTIONS)}")
        run = {"confirm": lambda i: self.confirm_match(i, reviewer),
               "approve": lambda i: self.set_review_state(i, "approved_for_sync", reviewer),
               "unlink": lambda i: self.reject_match(i, "bulk unlink", reviewer)}[action]
        done, failed, review_only = [], [], 0
        for match_id in ids:
            try:
                out = run(match_id)
                done.append(match_id)
                review_only += 1 if out.get("review_only") else 0
            except (ValueError, KeyError) as exc:
                row = self.db.one("SELECT pl_name, sl_name FROM matches WHERE id=?", (match_id,)) or {}
                failed.append({"id": match_id, "name": row.get("pl_name") or row.get("sl_name") or f"#{match_id}",
                               "error": str(exc.args[0]) if exc.args else str(exc)})
        self.db.audit(reviewer, f"bulk.{action}", "mapping", mapping_id,
                      {"requested": len(ids), "done": len(done), "failed": len(failed)}, now())
        return {"action": action, "requested": len(ids), "done": done, "failed": failed, "review_only": review_only}

    BULK_DECISIONS = (APPROVE_SL, ACCEPT_PL, FALSE_POSITIVE, "reject")

    def decide_many(self, match_id: int, field: str, entry_keys: Any, action: str,
                    reviewer: str = "reviewer") -> dict[str, Any]:
        """One decision for many rows of one table. Actions that need a value per row (correct,
        note_only) stay single-row; a false positive from here is remembered for this component."""
        if action not in self.BULK_DECISIONS:
            raise ValueError("bulk decisions are approve_sl, accept_pl, mark_false_positive or reject")
        if not isinstance(entry_keys, list) or not entry_keys:
            raise ValueError("entry_keys must be a non-empty list")
        done, failed = [], []
        for key in dict.fromkeys(str(k) for k in entry_keys):
            try:
                self.decide(match_id, field, key, action, reviewer=reviewer, scope="component", path_pattern="*")
                done.append(key)
            except ValueError as exc:
                failed.append({"entry_key": key, "error": str(exc)})
        return {"done": done, "failed": failed, **self.recompute(match_id)}

    # --- rules: one decision applied wherever its condition holds ------------
    RULES = {
        "copyright_formatting": {
            "label": "Approve SL for copyright notices that differ only in whitespace or case",
            "detail": "Only notices that are identical once whitespace and case are normalized. Near-identical "
                      "notices (a different year or holder) are left for a person.",
            "field": FIELD_COPYRIGHT, "action": APPROVE_SL,
        },
        "license_text_source": {
            "label": "Approve the identified source-file text where PL holds only the generic KB text",
            "detail": "Per license, and only where one license file was identified for it (in the snapshot or "
                      "loaded on demand). Components with several candidate files are left for a person.",
            "field": FIELD_LICENSE_TEXT, "action": APPROVE_SL,
        },
    }

    def _rule_targets(self, mapping_id: int, name: str) -> list[tuple[dict[str, Any], str]]:
        """(match, entry_key) pairs a rule would decide: undecided rows on settled matches only."""
        if name not in self.RULES:
            raise KeyError(f"rule {name!r} not found")
        rule = self.RULES[name]
        settled = [m for m in self.s._current_matches(mapping_id)
                   if m["pl_key"] and m["sl_key"] and m["status"] in ("auto", "confirmed")
                   and m.get("review_state") != "approved_for_sync"]
        # one bulk read for the whole mapping: per component it was ~10 queries, 18 s on 900 components
        inventory = self.s.mapping_inventory(mapping_id)
        license_map = self.s.license_map_lookup()
        fp_rules = self.db.query("SELECT * FROM false_positive_rules WHERE revoked_at IS NULL ORDER BY id")
        decisions = decisions_by_match(self.db, [m["id"] for m in settled])
        out = []
        for m in settled:
            cmp = self.comparison(m, inventory, license_map=license_map, rules=fp_rules)
            decided = self._decided_keys(cmp, decisions.get(m["id"], []))
            for key in self._rule_keys(name, cmp):
                if (rule["field"], key) not in decided:
                    out.append((m, key))
        return out

    @staticmethod
    def _rule_keys(name: str, cmp: dict[str, Any]) -> list[str]:
        if name == "copyright_formatting":
            return [r.key for r in cmp["copyright_rows"] if r.kind == "variant" and r.pl_text and r.sl_text
                    and copyright_key(r.pl_text) == copyright_key(r.sl_text)]
        if name == "license_text_source":
            return [row.discrepancy_key for row in cmp.get("text_rows") or []
                    if row.cmp.recommendation == "replace_with_source" and row.cmp.preferred
                    and row.cmp.pl_current and row.cmp.pl_current.kind == KIND_GENERIC
                    and sum(1 for c in row.cmp.candidates if c.side == "sl" and c.kind == KIND_SOURCE) == 1]
        return []

    def rules(self, mapping_id: int) -> list[dict[str, Any]]:
        """Every rule with what it would decide here now - the preview a reviewer approves."""
        out = []
        for name, rule in self.RULES.items():
            targets = self._rule_targets(mapping_id, name)
            names = sorted({m["pl_name"] or m["sl_name"] for m, _ in targets})
            out.append({"name": name, "label": rule["label"], "detail": rule["detail"], "field": rule["field"],
                        "action": rule["action"], "rows": len(targets), "components": len(names),
                        "sample": names[:8]})
        return out

    def apply_rule(self, mapping_id: int, name: str, reviewer: str = "reviewer") -> dict[str, Any]:
        rule = self.RULES.get(name)
        if not rule:
            raise KeyError(f"rule {name!r} not found")
        done, failed = 0, []
        for m, key in self._rule_targets(mapping_id, name):
            try:
                self.decide(m["id"], rule["field"], key, rule["action"], reviewer=reviewer, note=f"rule: {rule['label']}")
                done += 1
            except ValueError as exc:
                failed.append({"id": m["id"], "name": m["pl_name"] or m["sl_name"], "error": str(exc)})
        self.db.audit(reviewer, "rule.applied", "mapping", mapping_id, {"rule": name, "decided": done,
                                                                       "failed": len(failed)}, now())
        return {"rule": name, "decided": done, "failed": failed}

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

    @staticmethod
    def _prior_matches(prev_matches: list[dict[str, Any]], prior_of: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """{current PL key: the previous mapping's match row for the component it pairs with}."""
        by_key = {m["pl_key"]: m for m in prev_matches if m["pl_key"]}
        return {cur: by_key[prev["external_id"]] for cur, prev in prior_of.items() if prev["external_id"] in by_key}

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
        # The same component at a new version has a new key; the name rule still pairs it, so a
        # version bump reads as "changed" (and carries its decisions), not as a brand-new component.
        prior_of = pair_by_name(cur_pl_comps, prev_pl_comps)
        new = [c for c in cur_pl_comps if c["external_id"] not in prior_of]
        changed = [c for c in cur_pl_comps if c["external_id"] in prior_of
                   and c["source_version"] != prior_of[c["external_id"]]["source_version"]]
        unchanged = [c for c in cur_pl_comps if c["external_id"] in prior_of and c not in changed]
        prev_by_key = {c["external_id"]: prior_of[c["external_id"]] for c in cur_pl_comps if c["external_id"] in prior_of}

        cur_matches = self.s._current_matches(mapping_id)
        prev_matches = self.s._current_matches(previous["id"]) if previous else []
        cur_by_pl = {m["pl_key"]: m for m in cur_matches if m["pl_key"]}
        prev_by_pl = self._prior_matches(prev_matches, prior_of)
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
            prev_by_pl = self._prior_matches(prev_matches, prior_of)
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

        text_views = []
        rows = cmp.get("text_rows") or []
        for row in rows:
            tc = row.cmp
            mine = [d for (f, k), d in decided.items() if f == FIELD_LICENSE_TEXT and self._text_row(rows, k) is row]
            latest = max(mine, key=lambda d: d["id"]) if mine else None
            text_views.append({
                "license": row.license, "suffix": row.suffix, "discrepancy_key": row.discrepancy_key,
                "pl_current": asdict(tc.pl_current) if tc.pl_current else None,
                "candidates": [asdict(c) for c in tc.candidates],
                "unassigned": [asdict(c) for c in row.unassigned],
                "preferred": asdict(tc.preferred) if tc.preferred else None,
                "recommendation": tc.recommendation,
                "reasons": tc.reasons,
                "decision": latest,
                "rejected": bool(latest and latest["action"] in REJECTION_ACTIONS),
            })
        return copyright_rows, license_rows, text_views

    def _detail_warnings(self, match: dict[str, Any], cmp: dict[str, Any],
                         pl_comp: dict[str, Any] | None, sl_comp: dict[str, Any] | None) -> list[str]:
        warnings: list[str] = []
        if pl_comp and sl_comp:
            for side, comp in (("PL", pl_comp), ("SL", sl_comp)):
                if not comp["source_version"]:
                    warnings.append(f"{side} component has no observation hash")
        if match["status"] == "suggested":
            warnings.append("suggested match requires confirmation")
        if match["status"] == "unmatched":
            warnings.append("component is unmatched; confirm a manual match before reviewing")
        if cmp["text_cmp"] and cmp["text_cmp"].recommendation == "no_source_text":
            warnings.append("no identified source-file license text; component flagged for further review")
        if not pl_comp and match["pl_key"] and match["method"] == "manual" \
                and self.s.listing_component(match["mapping_id"], match["pl_key"]):
            warnings.append("PL details are not read yet: this hand-linked component was outside the SL scope; "
                            "refresh snapshots to read it")
        elif not pl_comp or not sl_comp:
            warnings.append("one side missing from the current snapshot")
        return warnings

    def _reassignment_candidates(self, match: dict[str, Any]) -> list[dict[str, Any]]:
        """Unmatched SL components ranked by the name rule - what a reassignment may pick."""
        candidates = []
        snap = self.s.current_snapshot(match["mapping_id"], "sl")
        matched_sl = {m["sl_key"] for m in self.s._current_matches(match["mapping_id"]) if m["sl_key"]}
        if snap:
            pk = name_key(match["pl_name"], match["pl_version"])
            for c in self.s.snapshot_components(snap["id"]):
                if c["external_id"] in matched_sl:
                    continue
                candidates.append({"sl_key": c["external_id"], "sl_name": c["name"], "sl_version": c["version"],
                                   "similarity": name_similarity(pk, name_key(c["name"], c["version"]))})
            candidates.sort(key=lambda x: -x["similarity"])
        return candidates

    def detail(self, match_id: int) -> dict[str, Any]:
        match = self.match(match_id)
        cmp = self.comparison(match)
        decisions = _decision_rows(self.db, match_id)
        notes = self.db.query("SELECT * FROM notes WHERE match_id=? ORDER BY id", (match_id,))
        rejected = {(d["field"], d["entry_key"]): d for d in decisions if d["action"] in REJECTION_ACTIONS}
        decided = {(d["field"], d["entry_key"]): d for d in decisions}
        copyright_rows, license_rows, text_views = self._decision_view(cmp, decided, rejected)
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
            "comparison": {"copyrights": copyright_rows, "licenses": license_rows,
                           "license_texts": text_views, "license_text": text_views[0] if text_views else None,
                           "loaded_files": [{k: f[k] for k in ("file_path", "licenses_json", "loaded_by", "loaded_at")}
                                            for f in cmp["loaded_files"]]},
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

    REVIEWER_SUPPLIED = "reviewer_supplied"

    def _license_text_value(self, cmp: dict[str, Any], key: str, action: str, value: str) -> str:
        """The text a license-text decision approves.

        Never invented: it is one of the identified SL source-file candidates of that license row
        (snapshot evidence or files loaded on demand), or text a reviewer typed in under the
        ``reviewer_supplied[@license]`` key, which ``decide`` only accepts with a note saying where
        it comes from.
        """
        rows = cmp.get("text_rows") or []
        base, lkey = split_text_key(key)
        row = self._text_row(rows, key)
        if lkey and row is None:
            raise ValueError(f"no PL license {lkey!r} on this component")
        if base == self.REVIEWER_SUPPLIED:
            if action != CORRECT or not value:
                raise ValueError("reviewer-supplied license text needs the text itself (action correct)")
            return value
        sources = {c.content_hash: c.text for c in ((row.cmp.candidates + row.unassigned) if row else [])
                   if c.side == "sl" and c.kind == KIND_SOURCE}
        if action in (APPROVE_SL, CORRECT):
            if base not in sources:
                raise ValueError("license text must come from an identified SL source-file candidate")
            return sources[base]
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
        if field == FIELD_LICENSE_TEXT and split_text_key(key)[0] == self.REVIEWER_SUPPLIED and not note.strip():
            raise ValueError("say where reviewer-supplied license text comes from (a note is required)")

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
        undecided = set(self._discrepancies(cmp, decisions)) - self._decided_keys(cmp, decisions)
        if state == "approved_for_sync":
            if undecided:
                raise ValueError(f"{len(undecided)} discrepant field(s) still undecided")
            review_only = not any(d["action"] in (APPROVE_SL, CORRECT) for d in decisions)
            if review_only:
                self._check_review_only(match)
        else:
            review_only = False
        with self.db.transaction():
            self.db.execute("UPDATE matches SET review_state=?, updated_at=? WHERE id=?", (state, now(), match_id))
            self.db.audit(reviewer, f"review.{state or 'reset'}", "match", match_id,
                          {"state": state, "review_only": review_only}, now())
            result = self.recompute(match_id)
        return {"ok": True, **result, "review_only": review_only}

    def _check_review_only(self, match: dict[str, Any]) -> None:
        """Approving a component with nothing to correct: the sync then only records the review.

        Allowed when both sides are present on a settled match and the PL tool can record a review
        status; otherwise there is truly nothing to synchronize.
        """
        if not match["pl_key"] or not match["sl_key"] or match["status"] in ("unmatched", "rejected"):
            raise ValueError("no approved SL-derived correction to synchronize")
        if match["status"] == "suggested":
            raise ValueError("confirm the suggested match before approving it as reviewed")
        mapping = self.s.mapping(match["mapping_id"])
        if not self.s.connector_for(mapping["pl_connection_id"]).capabilities.write_review_status:
            raise ValueError("no approved SL-derived correction to synchronize, and the PL tool cannot "
                             "record a review status")

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
        """Pair any PL component with any SL component on this row.

        A component taken from another row is released there (that row keeps its other side as an
        unmatched entry); a component this row gives up gets its own unmatched entry.
        """
        match = self.match(match_id)
        mid = match["mapping_id"]
        new_pl = pl_key if pl_key is not None else match["pl_key"]
        new_sl = sl_key if sl_key is not None else match["sl_key"]
        # A PL component outside the SL scope can be linked from its listing; the link keeps it in
        # scope, so the next refresh reads its details.
        pl_c = (self.s.component_by_key(mid, "pl", new_pl) or self.s.listing_component(mid, new_pl)) if new_pl else None
        if new_pl and not pl_c:
            raise ValueError(f"PL component {new_pl!r} not in current snapshot")
        if new_sl and not self.s.component_by_key(mid, "sl", new_sl):
            raise ValueError(f"SL component {new_sl!r} not in current snapshot")
        sl_c = self.s.component_by_key(mid, "sl", new_sl) if new_sl else None
        same = self.db.one("SELECT id, status FROM matches WHERE mapping_id=? AND pl_key=? AND sl_key=? AND id!=?",
                           (mid, new_pl, new_sl, match_id))
        if same and same["status"] != "rejected":
            raise ValueError(f"that pair is already matched in component #{same['id']}")
        changed = (match["pl_key"], match["sl_key"]) != (new_pl, new_sl)
        with self.db.transaction():
            if same:  # a rejected pair the reviewer now wants after all
                self.db.execute("DELETE FROM matches WHERE id=?", (same["id"],))
            touched = self._release_others(mid, match_id, new_pl, new_sl)
            if changed:
                for table in ("decisions", "suggestions"):
                    self.db.execute(f"DELETE FROM {table} WHERE match_id=?", (match_id,))
            self.db.execute(
                "UPDATE matches SET pl_key=?, sl_key=?, pl_name=?, pl_version=?, sl_name=?, sl_version=?,"
                " method='manual', confidence=1.0, status='confirmed', rationale_json=?, mismatch_json=?,"
                " review_state=CASE WHEN ? THEN NULL ELSE review_state END,"
                " confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?",
                (new_pl, new_sl, (pl_c or {}).get("name", ""), (pl_c or {}).get("version"),
                 (sl_c or {}).get("name", ""), (sl_c or {}).get("version"),
                 json.dumps(["manual assignment by reviewer"]),
                 json.dumps([f"PL {((pl_c or {}).get('name'))} {((pl_c or {}).get('version'))} vs SL "
                             f"{((sl_c or {}).get('name'))} {((sl_c or {}).get('version'))}"]),
                 1 if changed else 0, reviewer, now(), now(), match_id),
            )
            for side, old, new in (("pl", match["pl_key"], new_pl), ("sl", match["sl_key"], new_sl)):
                if old and old != new:
                    touched.append(self._lone_row(mid, side, old))
            self.db.audit(reviewer, "match.reassigned", "match", match_id,
                          {"pl_key": new_pl, "sl_key": new_sl}, now())
            result = self.recompute(match_id)
            for other in touched:
                if other:
                    self.recompute(other)
        return {"ok": True, **result}

    def _lone_row(self, mapping_id: int, side: str, key: str) -> int | None:
        """The unmatched row for a component that has no partner, created if it is missing."""
        comp = self.s.component_by_key(mapping_id, side, key)
        if not comp:
            return None
        pl_key, sl_key = (key, "") if side == "pl" else ("", key)
        reason = "no SL counterpart found" if side == "pl" else "no PL counterpart found"
        names = (comp["name"], comp["version"], "", None) if side == "pl" else ("", None, comp["name"], comp["version"])
        row = self.db.one("SELECT id FROM matches WHERE mapping_id=? AND pl_key=? AND sl_key=?",
                          (mapping_id, pl_key, sl_key))
        values = ("unmatched", json.dumps([reason]), *names)
        if row:
            self.db.execute(
                "UPDATE matches SET method='unmatched', confidence=0, status=?, rationale_json=?, mismatch_json='[]',"
                " pl_name=?, pl_version=?, sl_name=?, sl_version=?, review_state=NULL, updated_at=? WHERE id=?",
                (*values, now(), row["id"]))
            return row["id"]
        cur = self.db.execute(
            "INSERT INTO matches(mapping_id,pl_key,sl_key,method,confidence,status,rationale_json,"
            "pl_name,pl_version,sl_name,sl_version,created_at,updated_at) VALUES(?,?,?,'unmatched',0,?,?,?,?,?,?,?,?)",
            (mapping_id, pl_key, sl_key, *values, now(), now()))
        return cur.lastrowid

    def _release_others(self, mapping_id: int, match_id: int, pl_key: str, sl_key: str) -> list[int | None]:
        """Take pl_key / sl_key away from any other live row; the row keeps what it has left."""
        held = self.db.query(
            "SELECT * FROM matches WHERE mapping_id=? AND id!=? AND status!='rejected'"
            " AND ((pl_key!='' AND pl_key=?) OR (sl_key!='' AND sl_key=?))", (mapping_id, match_id, pl_key, sl_key))
        touched: list[int | None] = []
        for row in held:
            rest_pl = "" if row["pl_key"] == pl_key else row["pl_key"]
            rest_sl = "" if row["sl_key"] == sl_key else row["sl_key"]
            self.db.execute("DELETE FROM matches WHERE id=?", (row["id"],))
            if rest_pl:
                touched.append(self._lone_row(mapping_id, "pl", rest_pl))
            if rest_sl:
                touched.append(self._lone_row(mapping_id, "sl", rest_sl))
        return touched

    def create_match(self, mapping_id: int, pl_key: str, sl_key: str, reviewer: str = "reviewer") -> dict[str, Any]:
        """Manually pair a PL and an SL component, whatever the automatic matcher proposed for them."""
        if not pl_key or not sl_key:
            raise ValueError("pick both a PL and an SL component")
        self.s.mapping(mapping_id)
        row = (self.db.one("SELECT id FROM matches WHERE mapping_id=? AND pl_key=? AND sl_key=?",
                           (mapping_id, pl_key, sl_key))
               or self.db.one("SELECT id FROM matches WHERE mapping_id=? AND pl_key=? AND status!='rejected'",
                              (mapping_id, pl_key))
               or self.db.one("SELECT id FROM matches WHERE mapping_id=? AND sl_key=? AND status!='rejected'",
                              (mapping_id, sl_key)))
        if row:
            match_id = row["id"]
        else:
            cur = self.db.execute(
                "INSERT INTO matches(mapping_id,pl_key,sl_key,method,status,created_at,updated_at)"
                " VALUES(?,?,?,'manual','confirmed',?,?)", (mapping_id, pl_key, sl_key, now(), now()))
            match_id = cur.lastrowid
        out = self.reassign(match_id, pl_key=pl_key, sl_key=sl_key, reviewer=reviewer)
        return {**out, "match_id": match_id}

    def pool(self, mapping_id: int, q: str = "", limit: int = 200) -> dict[str, Any]:
        """Every component in the current snapshots, with the row each one currently sits in."""
        out: dict[str, Any] = {}
        ql = q.strip().lower()
        rows = self.db.query("SELECT id, pl_key, sl_key, status, pl_name, sl_name FROM matches WHERE mapping_id=?"
                             " AND status!='rejected'", (mapping_id,))
        for side in ("pl", "sl"):
            held = {r[f"{side}_key"]: r for r in rows if r[f"{side}_key"]}
            other = "sl" if side == "pl" else "pl"
            snap = self.s.current_snapshot(mapping_id, side)
            items = []
            for c in (self.s.snapshot_components(snap["id"]) if snap else []):
                if ql and not query_matches(ql, c["name"], c["version"], c["external_id"]):
                    continue
                m = held.get(c["external_id"])
                items.append({"key": c["external_id"], "name": c["name"], "version": c["version"],
                              "match_id": m["id"] if m else None,
                              "partner": (m[f"{other}_name"] or None) if m and m["status"] != "unmatched" else None})
            if side == "pl":
                # outside the SL scope: listed, never read, offered last so the likely picks come first
                for c in self.s.listing_components(mapping_id):
                    if c["external_id"] in held or (ql and not query_matches(ql, c["name"], c["version"], c["external_id"])):
                        continue
                    items.append({"key": c["external_id"], "name": c["name"], "version": c["version"],
                                  "match_id": None, "partner": None, "not_read": True})
            out[side] = {"total": len(items), "items": items[:limit]}
        return out

    def reject_match(self, match_id: int, reason: str = "", reviewer: str = "reviewer") -> dict[str, Any]:
        """Break the pair for good: it is not proposed again, and each side returns to the unmatched list."""
        match = self.match(match_id)
        with self.db.transaction():
            self.db.execute("UPDATE matches SET status='rejected', updated_at=? WHERE id=?", (now(), match_id))
            self.db.audit(reviewer, "match.rejected", "match", match_id, {"reason": reason}, now())
            result = self.recompute(match_id)
            if match["pl_key"] and match["sl_key"]:
                for side in ("pl", "sl"):
                    lone = self._lone_row(match["mapping_id"], side, match[f"{side}_key"])
                    if lone:
                        self.recompute(lone)
        return {"ok": True, **result}
