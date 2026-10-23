"""Review workflow: queue, component workspace, field-level decisions, notes, statuses."""
from __future__ import annotations

import json
from dataclasses import asdict
from difflib import SequenceMatcher
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
    IRRELEVANT,
    KIND_SOURCE,
    NOTE_ONLY,
    REJECT,
    REJECTION_ACTIONS,
    FALSE_POSITIVE,
    copyright_key,
    norm_license,
    norm_name,
    sha256,
)
from .service import Service, _ce, _le, _te, now


def _json(row: dict[str, Any], key: str) -> list[Any]:
    return json.loads(row.get(key) or "[]")


def _decision_rows(db: DB, match_id: int) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM decisions WHERE match_id=? ORDER BY field, entry_key", (match_id,))


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

    def _component(self, mapping_id: int, side: str, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        row = self.s.component_by_key(mapping_id, side, key)
        if not row:
            return None
        obs = self.s.component_observations(row["id"])
        return {"component": row, **obs}

    def comparison(self, match: dict[str, Any]) -> dict[str, Any]:
        mid = match["mapping_id"]
        pl = self._component(mid, "pl", match["pl_key"])
        sl = self._component(mid, "sl", match["sl_key"])
        pl_c = [_ce(r) for r in (pl or {}).get("copyrights", [])]
        sl_c = [_ce(r) for r in (sl or {}).get("copyrights", [])]
        pl_l = [_le(r) for r in (pl or {}).get("licenses", [])]
        sl_l = [_le(r) for r in (sl or {}).get("licenses", [])]
        pl_t = [_te(r) for r in (pl or {}).get("license_texts", [])]
        sl_t = [_te(r) for r in (sl or {}).get("license_texts", [])]
        copyright_rows = compare_copyrights(pl_c, sl_c) if (pl_c or sl_c) else []
        license_rows = compare_licenses(pl_l, sl_l) if (pl_l or sl_l) else []
        text_cmp = compare_license_texts(pl_t, sl_t) if (pl_t or sl_t) else None
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
            if r.kind in ("sl_only", "pl_only", "spdx_diff") and (FIELD_LICENSE_NAME, r.key) not in rejected:
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

    def recompute(self, match_id: int) -> dict[str, Any]:
        match = self.match(match_id)
        decisions = _decision_rows(self.db, match_id)
        cmp = self.comparison(match)
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
    def list_components(self, mapping_id: int, *, q: str = "", review_status: str = "", match_status: str = "",
                        confidence: str = "", sort: str = "name", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        rows = self.s._current_matches(mapping_id)
        lic_names: dict[tuple[str, str], list[str]] = {}
        ident: dict[tuple[str, str], list[str]] = {}
        for side in ("pl", "sl"):
            snap = self.s.current_snapshot(mapping_id, side)
            if not snap:
                continue
            for r in self.db.query(
                "SELECT c.external_id AS k, l.name AS n FROM licenses l JOIN components c ON c.id=l.component_id"
                " WHERE c.snapshot_id=? AND l.active=1", (snap["id"],)
            ):
                lic_names.setdefault((side, r["k"]), []).append(r["n"])
            for r in self.db.query("SELECT external_id AS k, purl, metadata_json FROM components WHERE snapshot_id=?",
                                   (snap["id"],)):
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
        ql = q.strip().lower()
        out: list[dict[str, Any]] = []
        for m in rows:
            if match_status and m["status"] != match_status:
                continue
            if review_status and m["review_status"] != review_status:
                continue
            if confidence == "high" and m["confidence"] < 0.9:
                continue
            if confidence == "low" and m["confidence"] >= 0.9:
                continue
            hay = " ".join(filter(None, [
                m["pl_name"], m["pl_version"], m["sl_name"], m["sl_version"], m["method"],
                m["pl_key"], m["sl_key"],
                *(lic_names.get(("pl", m["pl_key"]), [])), *(lic_names.get(("sl", m["sl_key"]), [])),
                *(ident.get(("pl", m["pl_key"]), [])), *(ident.get(("sl", m["sl_key"]), [])),
            ])).lower()
            if ql and ql not in hay:
                continue
            out.append({
                "id": m["id"], "pl_name": m["pl_name"], "pl_version": m["pl_version"],
                "sl_name": m["sl_name"], "sl_version": m["sl_version"],
                "method": m["method"], "confidence": m["confidence"], "status": m["status"],
                "review_status": m["review_status"], "discrepancy_count": m["discrepancy_count"],
                "mismatch_reasons": json.loads(m.get("mismatch_json") or "[]"),
                "licenses": sorted(set(lic_names.get(("pl", m["pl_key"]), []) + lic_names.get(("sl", m["sl_key"]), []))),
            })
        key = {
            "name": lambda r: (r["pl_name"].lower(), r["pl_version"] or ""),
            "confidence": lambda r: -r["confidence"],
            "discrepancies": lambda r: -r["discrepancy_count"],
            "status": lambda r: (r["review_status"], r["pl_name"].lower()),
        }.get(sort, lambda r: (r["pl_name"].lower(), r["pl_version"] or ""))
        out.sort(key=key)
        total = len(out)
        page = out[offset: offset + limit]
        return {"total": total, "offset": offset, "limit": limit, "items": page}

    # --- workspace ---------------------------------------------------------
    def detail(self, match_id: int) -> dict[str, Any]:
        match = self.match(match_id)
        cmp = self.comparison(match)
        decisions = _decision_rows(self.db, match_id)
        notes = self.db.query("SELECT * FROM notes WHERE match_id=? ORDER BY id", (match_id,))
        rejected = {(d["field"], d["entry_key"]): d for d in decisions if d["action"] in REJECTION_ACTIONS}
        decided = {(d["field"], d["entry_key"]): d for d in decisions}

        copyright_rows = []
        for r in cmp["copyright_rows"]:
            d = decided.get((FIELD_COPYRIGHT, r.key))
            copyright_rows.append({**asdict(r), "decision": d,
                                   "rejected": bool(rejected.get((FIELD_COPYRIGHT, r.key)))})
        license_rows = []
        for r in cmp["license_rows"]:
            d = decided.get((FIELD_LICENSE_NAME, r.key))
            license_rows.append({**asdict(r), "decision": d,
                                 "rejected": bool(rejected.get((FIELD_LICENSE_NAME, r.key)))})

        text_view = None
        tc = cmp["text_cmp"]
        if tc:
            text_view = {
                "pl_current": asdict(tc.pl_current) if tc.pl_current else None,
                "candidates": [asdict(c) for c in tc.candidates],
                "preferred": asdict(tc.preferred) if tc.preferred else None,
                "recommendation": tc.recommendation,
                "reasons": tc.reasons,
                "decision": decided.get((FIELD_LICENSE_TEXT, tc.preferred.content_hash if tc.preferred else "no_source_text")),
                "rejected": bool(rejected.get((FIELD_LICENSE_TEXT, tc.preferred.content_hash if tc.preferred else "no_source_text"))),
            }

        warnings: list[str] = []
        pl_comp = cmp["pl"]["component"] if cmp["pl"] else None
        sl_comp = cmp["sl"]["component"] if cmp["sl"] else None
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

        stale = self._stale_flags(match, decisions, pl_comp, sl_comp)
        from .sync import preview_match  # local import keeps the module graph acyclic
        preview = preview_match(self.s, match, decisions) if pl_comp and sl_comp else []

        candidates = []
        if match["status"] == "unmatched" or match["status"] == "rejected":
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

        return {
            "match": match,
            "pl": cmp["pl"], "sl": cmp["sl"],
            "comparison": {"copyrights": copyright_rows, "licenses": license_rows, "license_text": text_view},
            "decisions": decisions, "notes": notes, "suggestions": self.suggestions(match_id),
            "review_status": match["review_status"], "discrepancy_count": match["discrepancy_count"],
            "sync_preview": preview, "stale": stale, "warnings": warnings, "candidates": candidates,
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

    def decide(self, match_id: int, field: str, entry_key: str, action: str, *,
               approved_value: str | None = None, note: str = "", reviewer: str = "reviewer") -> dict[str, Any]:
        if field not in FIELDS:
            raise ValueError(f"unknown field {field!r}")
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        match = self.match(match_id)
        cmp = self.comparison(match)
        key = entry_key or ""
        value = (approved_value or "").strip()

        if field == FIELD_COPYRIGHT:
            pl_texts = {copyright_key(r["text"]): r["text"] for r in (cmp["pl"] or {}).get("copyrights", [])}
            sl_texts = {copyright_key(r["text"]): r["text"] for r in (cmp["sl"] or {}).get("copyrights", [])}
            rows = {r.key: r for r in cmp["copyright_rows"]}
            if action == APPROVE_SL:
                if key in sl_texts:
                    value = sl_texts[key]
                elif rows.get(key) is not None and rows[key].sl_text:
                    value = rows[key].sl_text
                else:
                    raise ValueError("approve_sl requires an SL copyright entry key")
            elif action == ACCEPT_PL:
                if key in pl_texts:
                    value = pl_texts[key]
                elif rows.get(key) is not None and rows[key].pl_text:
                    value = rows[key].pl_text
                else:
                    raise ValueError("accept_pl requires a PL copyright entry key")
            elif action == CORRECT:
                if not value:
                    raise ValueError("correct requires approved_value")
            elif key and key not in sl_texts and key not in pl_texts and key not in rows:
                raise ValueError("entry_key does not exist in either side")
        elif field == FIELD_LICENSE_NAME:
            pl_names = {norm_license(r["name"]): r["name"] for r in (cmp["pl"] or {}).get("licenses", [])}
            sl_names = {norm_license(r["name"]): r["name"] for r in (cmp["sl"] or {}).get("licenses", [])}
            if action == APPROVE_SL:
                if key not in sl_names:
                    raise ValueError("approve_sl requires an SL license entry key")
                value = sl_names[key]
            elif action == ACCEPT_PL:
                if key not in pl_names:
                    raise ValueError("accept_pl requires a PL license entry key")
                value = pl_names[key]
            elif action == CORRECT and not value:
                raise ValueError("correct requires approved_value")
            elif action not in (APPROVE_SL, ACCEPT_PL, CORRECT) and key not in sl_names and key not in pl_names:
                raise ValueError("entry_key does not exist in either side")
        else:  # license text: never invent text
            tc = cmp["text_cmp"]
            sources = {c.content_hash: c.text for c in (tc.candidates if tc else [])
                       if c.side == "sl" and c.kind == KIND_SOURCE and c.is_license_file}
            if action in (APPROVE_SL, CORRECT):
                if key not in sources:
                    raise ValueError("license text must come from an identified SL source-file candidate")
                value = sources[key]
            if action == ACCEPT_PL and value is None:
                value = None

        if action == NOTE_ONLY and not note:
            raise ValueError("note_only requires a note")

        src = self._sl_source_version(match) if action in (APPROVE_SL, CORRECT) else self._pl_source_version(match)
        pl_src = self._pl_source_version(match)
        self.db.execute(
            "INSERT INTO decisions(mapping_id,match_id,field,entry_key,action,approved_value,reviewer,note,"
            "source_version,pl_source_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(match_id,field,entry_key) DO UPDATE SET action=excluded.action,"
            " approved_value=excluded.approved_value, reviewer=excluded.reviewer, note=excluded.note,"
            " source_version=excluded.source_version, pl_source_version=excluded.pl_source_version,"
            " created_at=excluded.created_at",
            (match["mapping_id"], match_id, field, key, action, value or None, reviewer, note, src, pl_src, now()),
        )
        self.db.audit(reviewer, f"decision.{action}", "match", match_id,
                      {"field": field, "entry_key": key, "approved_value": value}, now())
        result = self.recompute(match_id)
        return {"ok": True, **result, "decision": self.db.one(
            "SELECT * FROM decisions WHERE match_id=? AND field=? AND entry_key=?", (match_id, field, key))}

    # --- machine suggestions (future LLM assistance; never auto-approved) --
    def add_suggestion(self, match_id: int, field: str, entry_key: str, value: str, *,
                       confidence: float = 0.0, model: str = "", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        if field not in FIELDS:
            raise ValueError(f"unknown field {field!r}")
        match = self.match(match_id)
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
            self.db.execute("UPDATE suggestions SET status='rejected', decided_by=?, decided_at=? WHERE id=?",
                            (reviewer, now(), suggestion_id))
            self.db.audit(reviewer, "suggestion.rejected", "suggestion", suggestion_id, {"model": row["model"]}, now())
            return {"ok": True, "status": "rejected"}
        # acceptance always becomes a human decision, validated like any other decision
        result = self.decide(row["match_id"], row["field"], row["entry_key"], APPROVE_SL,
                             approved_value=row["value"], note=note, reviewer=reviewer)
        self.db.execute("UPDATE suggestions SET status='accepted', decided_by=?, decided_at=? WHERE id=?",
                        (reviewer, now(), suggestion_id))
        self.db.audit(reviewer, "suggestion.accepted", "suggestion", suggestion_id,
                      {"model": row["model"], "field": row["field"]}, now())
        return {"ok": True, "status": "accepted", **result}

    def add_note(self, match_id: int, body: str, reviewer: str = "reviewer") -> dict[str, Any]:
        match = self.match(match_id)
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
        self.db.execute("UPDATE matches SET review_state=?, updated_at=? WHERE id=?", (state, now(), match_id))
        self.db.audit(reviewer, f"review.{state or 'reset'}", "match", match_id, {"state": state}, now())
        return {"ok": True, **self.recompute(match_id)}

    # --- match management --------------------------------------------------
    def confirm_match(self, match_id: int, reviewer: str = "reviewer") -> dict[str, Any]:
        match = self.match(match_id)
        if not match["pl_key"] or not match["sl_key"]:
            raise ValueError("both sides must be assigned before confirmation")
        self.db.execute("UPDATE matches SET status='confirmed', confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?",
                        (reviewer, now(), now(), match_id))
        self.db.audit(reviewer, "match.confirmed", "match", match_id,
                      {"method": match["method"], "confidence": match["confidence"]}, now())
        return {"ok": True, **self.recompute(match_id)}

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
        self.db.audit(reviewer, "match.reassigned", "match", match_id, {"pl_key": new_pl, "sl_key": new_sl}, now())
        return {"ok": True, **self.recompute(match_id)}

    def reject_match(self, match_id: int, reason: str = "", reviewer: str = "reviewer") -> dict[str, Any]:
        self.db.execute("UPDATE matches SET status='rejected', updated_at=? WHERE id=?", (now(), match_id))
        self.db.audit(reviewer, "match.rejected", "match", match_id, {"reason": reason}, now())
        return {"ok": True, **self.recompute(match_id)}
