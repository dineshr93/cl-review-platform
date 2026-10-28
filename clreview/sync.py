"""One-way synchronization: preview, dry-run, controlled writes, verification, retry.

Only decisions explicitly approved for synchronization are candidates. Each item
is validated against the target connector's declared capabilities; unsupported
fields are reported, never faked.
"""
from __future__ import annotations

import json
from typing import Any

from .connectors.base import ConnectorError
from .connectors.http import UnsupportedOperation
from .domain import (
    FIELD_COPYRIGHT,
    FIELD_LICENSE_NAME,
    FIELD_LICENSE_TEXT,
    SYNC_ACTIONS,
    sha256,
)
from .service import Service, now

MAX_SYNC_ITEMS_DEFAULT = 500


def _pl_component(s: Service, mapping_id: int, key: str) -> dict[str, Any] | None:
    return s.component_by_key(mapping_id, "pl", key) if key else None


def _sl_text(s: Service, mapping_id: int, sl_key: str, content_hash: str) -> dict[str, Any] | None:
    comp = s.component_by_key(mapping_id, "sl", sl_key) if sl_key else None
    if not comp:
        return None
    return s.db.one("SELECT * FROM license_texts WHERE component_id=? AND content_hash=?",
                    (comp["id"], content_hash))


def _pl_observations(s: Service, mapping_id: int, key: str) -> dict[str, Any]:
    comp = _pl_component(s, mapping_id, key)
    if not comp:
        return {"copyrights": [], "licenses": [], "license_texts": []}
    return s.component_observations(comp["id"])


def preview_match(s: Service, match: dict[str, Any], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preview a single component's approved changes against the target PL."""
    if not match["pl_key"]:
        return []
    mapping = s.mapping(match["mapping_id"])
    caps = s.connector_for(mapping["pl_connection_id"]).capabilities
    obs = _pl_observations(s, match["mapping_id"], match["pl_key"])
    pl_comp = _pl_component(s, match["mapping_id"], match["pl_key"])
    current_pl_version = (pl_comp or {}).get("source_version", "")
    current_sl_version = (s.component_by_key(match["mapping_id"], "sl", match["sl_key"]) or {}).get("source_version", "") \
        if match["sl_key"] else ""
    out: list[dict[str, Any]] = []
    approved = [d for d in decisions if d["action"] in SYNC_ACTIONS and d["approved_value"]]
    for d in approved:
        item = {
            "field": d["field"], "entry_key": d["entry_key"], "approved_value": d["approved_value"],
            "decision_id": d["id"], "status": "ready", "message": "",
            "pl_before": None, "approved_for_sync": match.get("review_state") == "approved_for_sync",
        }
        if d["field"] == FIELD_COPYRIGHT:
            item["pl_before"] = [r["text"] for r in obs["copyrights"]]
            if d["approved_value"] in item["pl_before"]:
                item["status"] = "already_applied"
            if not caps.write_copyrights:
                item["status"] = "unsupported_target"
                item["message"] = "unsupported by target: " + (caps.notes or "copyright statements")
        elif d["field"] == FIELD_LICENSE_NAME:
            names = [r["name"] for r in obs["licenses"]]
            item["pl_before"] = names
            if d["approved_value"] in names:
                item["status"] = "already_applied"
            if not caps.write_license_names:
                item["status"] = "unsupported_target"
                item["message"] = "unsupported by target: " + (caps.notes or "license names")
        elif d["field"] == FIELD_LICENSE_TEXT:
            texts = obs["license_texts"]
            before = texts[0] if texts else None
            item["pl_before"] = ({"kind": before["kind"], "content_hash": before["content_hash"],
                                  "file_name": before["file_name"]} if before else None)
            if before and before["content_hash"] == sha256(d["approved_value"]):
                item["status"] = "already_applied"
            if not caps.write_license_text:
                item["status"] = "unsupported_target"
                item["message"] = "unsupported by target: " + (caps.notes or "component-specific license text")
        if d["source_version"] and current_sl_version and d["source_version"] != current_sl_version:
            item["status"] = "stale"
            item["message"] = "SL observations changed after the review decision"
        if d.get("pl_source_version") and current_pl_version and d["pl_source_version"] != current_pl_version:
            item["status"] = "conflict"
            item["message"] = "PL values changed after the review decision"
        out.append(item)
    return out


class SyncService:
    def __init__(self, service: Service, max_items: int = MAX_SYNC_ITEMS_DEFAULT):
        self.s = service
        self.db = service.db
        self.max_items = max_items

    def create_job(self, mapping_id: int, *, dry_run: bool, component_ids: list[int] | None = None,
                   confirm: bool = False, actor: str = "reviewer") -> dict[str, Any]:
        mapping = self.s.mapping(mapping_id)
        if not dry_run:
            if not confirm:
                raise ValueError("live synchronization requires confirm=true")
            conn = mapping["pl_connection"]
            if not conn.get("last_test_ok"):
                raise ValueError("PL connection has not passed a connectivity test; synchronization stays disabled")
        pl_conn = self.s.connector_for(mapping["pl_connection_id"])
        matches = self.s._current_matches(mapping_id)
        if component_ids:
            matches = [m for m in matches if m["id"] in set(component_ids)]
        candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for m in matches:
            if m.get("review_state") != "approved_for_sync":
                continue
            decisions = self.db.query("SELECT * FROM decisions WHERE match_id=?", (m["id"],))
            items = preview_match(self.s, m, decisions)
            if items:
                candidates.append((m, items))
        total = sum(len(items) for _, items in candidates)
        if total == 0:
            raise ValueError("no component is approved for synchronization")
        if total > self.max_items:
            raise ValueError(f"{total} items exceeds the {self.max_items}-item safety limit; narrow the selection")

        with self.db.transaction():
            job_id = self.db.execute(
                "INSERT INTO sync_jobs(mapping_id,dry_run,status,created_by,created_at) VALUES(?,?,?,?,?)",
                (mapping_id, 1 if dry_run else 0, "pending", actor, now()),
            ).lastrowid
            for m, items in candidates:
                for item in items:
                    self.db.execute(
                        "INSERT INTO sync_items(job_id,match_id,field,entry_key,status,pl_before,approved_value,"
                        "message,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (job_id, m["id"], item["field"], item["entry_key"], item["status"],
                         json.dumps(item["pl_before"]), item["approved_value"], item["message"], now()),
                    )
            self.db.audit(actor, "sync.job_created", "sync_job", job_id,
                          {"mapping_id": mapping_id, "dry_run": dry_run, "items": total}, now())
            if dry_run:
                self.db.execute("UPDATE sync_jobs SET status='dry_run', finished_at=? WHERE id=?", (now(), job_id))
        if not dry_run:
            # the PL round trip stays outside the transaction: the write lock must not be held across it
            self._execute(job_id, mapping, pl_conn, actor)
        return self.job(job_id)

    def _execute(self, job_id: int, mapping: dict[str, Any], pl_conn: Any, actor: str) -> None:
        self.db.execute("UPDATE sync_jobs SET status='in_progress' WHERE id=?", (job_id,))
        items = self.db.query("SELECT * FROM sync_items WHERE job_id=? ORDER BY id", (job_id,))
        caps = pl_conn.capabilities
        for item in items:
            status = item["status"]
            message = item["message"] or ""
            if status in ("unsupported_target", "stale", "conflict", "already_applied"):
                self.db.execute("UPDATE sync_items SET status=?, updated_at=? WHERE id=?",
                                (status, now(), item["id"]))
                continue
            match = self.db.one("SELECT * FROM matches WHERE id=?", (item["match_id"],))
            pl_key = (match or {}).get("pl_key", "")
            pl_row = _pl_component(self.s, match["mapping_id"], pl_key) if match else None
            if not pl_row or not pl_row.get("write_key"):
                self._fail(item["id"], status="failed",
                           message="target tool exposes no writable handle for this component")
                continue
            pl_key = pl_row["write_key"]
            try:
                current = pl_conn.read_component(pl_key)
            except UnsupportedOperation as exc:
                current = None
                message = str(exc)
            except ConnectorError as exc:
                self._fail(item["id"], status="failed", message=str(exc))
                continue

            try:
                if item["field"] == FIELD_COPYRIGHT:
                    if not caps.write_copyrights:
                        raise UnsupportedOperation(caps.notes or "copyright writes unsupported")
                    notices = [n for n in (current or {}).get("copyrights", [])]
                    if item["approved_value"] in notices:
                        self._skip(item["id"], notices, "notice already present in PL")
                        continue
                    result = pl_conn.update_copyrights(pl_key, notices + [item["approved_value"]])
                elif item["field"] == FIELD_LICENSE_NAME:
                    if not caps.write_license_names:
                        raise UnsupportedOperation(caps.notes or "license name writes unsupported")
                    names = [n for n in (current or {}).get("licenses", [])]
                    if item["approved_value"] in names:
                        self._skip(item["id"], names, "license already present in PL")
                        continue
                    desired = names + [item["approved_value"]]
                    result = pl_conn.update_licenses(pl_key, [{"name": n} for n in desired])
                else:
                    if not caps.write_license_text:
                        raise UnsupportedOperation(caps.notes or "license text writes unsupported")
                    before = (current or {}).get("license_texts") or []
                    if before and before[0].get("content_hash") == sha256(item["approved_value"]):
                        self._skip(item["id"], before, "PL already stores this license text")
                        continue
                    file_name = None
                    if match:
                        sl_row = _sl_text(self.s, match["mapping_id"], match["sl_key"], item["entry_key"])
                        file_name = (sl_row or {}).get("file_name")
                    result = pl_conn.update_license_text(pl_key, item["approved_value"], file_name)
            except UnsupportedOperation as exc:
                self.db.execute(
                    "UPDATE sync_items SET status='unsupported_target', message=?, attempts=attempts+1, updated_at=? WHERE id=?",
                    (str(exc), now(), item["id"]))
                continue
            except ConnectorError as exc:
                self._fail(item["id"], status="failed", message=str(exc), api_status=exc.status)
                continue

            api_status = result.get("status") if isinstance(result, dict) else None
            verified = None
            ok = isinstance(api_status, int) and 200 <= api_status < 300
            verify_message = ""
            if ok and caps.verify_writes:
                try:
                    after = pl_conn.read_component(pl_key)
                    verified = json.dumps(after)
                    ok = self._verified(item, after)
                    if not ok:
                        verify_message = "verification failed: PL value does not match the approved value"
                except (ConnectorError, UnsupportedOperation) as exc:
                    verified = f"verification failed: {exc}"
                    verify_message = f"verification failed: {exc}"
                    ok = False
            self.db.execute(
                "UPDATE sync_items SET status=?, api_status=?, api_response=?, message=?, verified_value=?,"
                " attempts=attempts+1, updated_at=? WHERE id=?",
                ("succeeded" if ok else "failed", api_status, json.dumps(result),
                 "" if ok else (message or verify_message or "write reported failure"), verified, now(), item["id"]),
            )
            self.db.audit(actor, "sync.item", "sync_item", item["id"],
                          {"field": item["field"], "status": "succeeded" if ok else "failed"}, now())
        self._finish(job_id)

    def _verified(self, item: dict[str, Any], after: dict[str, Any]) -> bool:
        if item["field"] == FIELD_COPYRIGHT:
            return item["approved_value"] in (after.get("copyrights") or [])
        if item["field"] == FIELD_LICENSE_NAME:
            return item["approved_value"] in (after.get("licenses") or [])
        texts = after.get("license_texts") or []
        return bool(texts and texts[0].get("content_hash") == sha256(item["approved_value"]))

    def _skip(self, item_id: int, before: Any, message: str) -> None:
        self.db.execute(
            "UPDATE sync_items SET status='skipped_already_applied', pl_before=?, message=?, updated_at=? WHERE id=?",
            (json.dumps(before), message, now(), item_id),
        )

    def _fail(self, item_id: int, *, status: str, message: str, api_status: int | None = None) -> None:
        self.db.execute(
            "UPDATE sync_items SET status=?, message=?, api_status=?, attempts=attempts+1, updated_at=? WHERE id=?",
            (status, message, api_status, now(), item_id),
        )

    def _finish(self, job_id: int) -> None:
        items = self.db.query("SELECT status FROM sync_items WHERE job_id=?", (job_id,))
        counts: dict[str, int] = {}
        for i in items:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        failed = counts.get("failed", 0)
        status = "failed" if failed and failed == len(items) else ("partial" if failed else "completed")
        self.db.execute("UPDATE sync_jobs SET status=?, finished_at=?, summary_json=? WHERE id=?",
                        (status, now(), json.dumps(counts), job_id))

    def job(self, job_id: int) -> dict[str, Any]:
        job = self.db.one("SELECT * FROM sync_jobs WHERE id=?", (job_id,))
        if not job:
            raise KeyError(f"sync job {job_id} not found")
        items = self.db.query("SELECT * FROM sync_items WHERE job_id=? ORDER BY id", (job_id,))
        by_component: dict[int, dict[str, Any]] = {}
        for i in items:
            i["pl_before"] = json.loads(i["pl_before"]) if i["pl_before"] else None
            m = self.db.one("SELECT pl_name,pl_version,sl_name,sl_version FROM matches WHERE id=?", (i["match_id"],))
            comp = by_component.setdefault(i["match_id"], {
                "match_id": i["match_id"], "pl_name": (m or {}).get("pl_name"),
                "pl_version": (m or {}).get("pl_version"), "items": [], "counts": {},
            })
            comp["items"].append(i)
            comp["counts"][i["status"]] = comp["counts"].get(i["status"], 0) + 1
        job["summary"] = json.loads(job.pop("summary_json") or "{}")
        job["dry_run"] = bool(job["dry_run"])
        job["items"] = items
        job["components"] = list(by_component.values())
        return job

    def list_jobs(self, mapping_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if mapping_id:
            rows = self.db.query("SELECT id FROM sync_jobs WHERE mapping_id=? ORDER BY id DESC LIMIT ?",
                                 (mapping_id, limit))
        else:
            rows = self.db.query("SELECT id FROM sync_jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [self.job(r["id"]) for r in rows]

    def retry(self, job_id: int, *, confirm: bool = False, actor: str = "reviewer") -> dict[str, Any]:
        job = self.job(job_id)
        if job["dry_run"]:
            raise ValueError("dry-run jobs have nothing to retry; create a live job")
        if not confirm:
            raise ValueError("retry requires confirm=true")
        failed = [i for i in job["items"] if i["status"] == "failed"]
        if not failed:
            raise ValueError("no failed items to retry")
        new_id = self.db.execute(
            "INSERT INTO sync_jobs(mapping_id,dry_run,status,created_by,created_at) VALUES(?,0,'pending',?,?)",
            (job["mapping_id"], actor, now()),
        ).lastrowid
        for i in failed:
            self.db.execute(
                "INSERT INTO sync_items(job_id,match_id,field,entry_key,status,pl_before,approved_value,message,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id, i["match_id"], i["field"], i["entry_key"], "ready", json.dumps(i["pl_before"]),
                 i["approved_value"], "retry of item %s" % i["id"], now()),
            )
        mapping = self.s.mapping(job["mapping_id"])
        pl_conn = self.s.connector_for(mapping["pl_connection_id"])
        self.db.audit(actor, "sync.retry", "sync_job", new_id, {"from": job_id, "items": len(failed)}, now())
        self._execute(new_id, mapping, pl_conn, actor)
        return self.job(new_id)
