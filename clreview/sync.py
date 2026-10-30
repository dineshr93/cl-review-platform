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
from .db import DB
from .domain import (
    FIELD_COPYRIGHT,
    FIELD_LICENSE_NAME,
    FIELD_LICENSE_TEXT,
    RETRYABLE_ITEM_STATUSES,
    SYNC_ACTIONS,
    sha256,
)
from .review import decisions_by_match
from .service import Service, _chunks, now

MAX_SYNC_ITEMS_DEFAULT = 500


def _pl_component(s: Service, mapping_id: int, key: str) -> dict[str, Any] | None:
    return s.component_by_key(mapping_id, "pl", key) if key else None


def _sl_text(s: Service, mapping_id: int, sl_key: str, content_hash: str) -> dict[str, Any] | None:
    comp = s.component_by_key(mapping_id, "sl", sl_key) if sl_key else None
    if not comp:
        return None
    return s.db.one("SELECT * FROM license_texts WHERE component_id=? AND content_hash=?",
                    (comp["id"], content_hash))


class PreviewContext:
    """What a whole preview needs, read once instead of once per component.

    Built for one call frame and passed down: the mapping, the target capabilities and both
    sides' components with their observations. A single-component caller can hand in the rows it
    already read through ``sides`` and cost no reads of its own.
    """

    def __init__(self, s: Service, mapping_id: int, sides: dict[str, dict[str, Any]] | None = None):
        self.mapping = s.mapping(mapping_id)
        self.caps = s.connector_for(self.mapping["pl_connection_id"]).capabilities
        self.sides = sides if sides is not None else {
            side: s.side_components_with_observations(mapping_id, side) for side in ("pl", "sl")}

    def source_version(self, side: str, key: str) -> str:
        if not key:
            return ""
        entry = self.sides.get(side, {}).get(key) or {}
        return (entry.get("component") or {}).get("source_version", "")

    def observations(self, side: str, key: str) -> dict[str, Any]:
        empty = {"copyrights": [], "licenses": [], "license_texts": []}
        if not key:
            return empty
        entry = self.sides.get(side, {}).get(key)
        if not entry:
            return empty
        return {k: entry.get(k, []) for k in ("copyrights", "licenses", "license_texts")}


def preview_match(s: Service, match: dict[str, Any], decisions: list[dict[str, Any]],
                  ctx: PreviewContext | None = None) -> list[dict[str, Any]]:
    """Preview a single component's approved changes against the target PL."""
    if not match["pl_key"]:
        return []
    ctx = ctx or PreviewContext(s, match["mapping_id"])
    caps = ctx.caps
    obs = ctx.observations("pl", match["pl_key"])
    current_pl_version = ctx.source_version("pl", match["pl_key"])
    current_sl_version = ctx.source_version("sl", match["sl_key"])
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

    def preview(self, mapping_id: int, component_ids: list[int] | None = None) -> dict[str, Any]:
        """Every approved change for a mapping (or for the named matches), read once.

        The whole preview costs a fixed handful of reads: the mapping, the target capabilities,
        both sides' components with their observations, and one batched decisions read. It used to
        cost a query per component, which made the button that answers "what would sync?" the most
        expensive call in the app.
        """
        matches = self.s._current_matches(mapping_id)
        if component_ids:
            matches = [m for m in matches if m["id"] in set(component_ids)]
        decided = decisions_by_match(self.db, [m["id"] for m in matches])
        ctx = PreviewContext(self.s, mapping_id)
        out = []
        for match in matches:
            items = preview_match(self.s, match, decided.get(match["id"], []), ctx)
            if items:
                out.append({"match": {"id": match["id"], "pl_name": match["pl_name"],
                                      "pl_version": match["pl_version"], "sl_name": match["sl_name"],
                                      "sl_version": match["sl_version"],
                                      "approved_for_sync": match.get("review_state") == "approved_for_sync"},
                            "items": items})
        return {"mapping_id": mapping_id, "components": out}

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
        approved = [m for m in matches if m.get("review_state") == "approved_for_sync"]
        ctx = PreviewContext(self.s, mapping_id)
        decided = decisions_by_match(self.db, [m["id"] for m in approved])
        candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for m in approved:
            items = preview_match(self.s, m, decided.get(m["id"], []), ctx)
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
            with self.db.transaction():
                self.db.execute(
                    "UPDATE sync_items SET status=?, api_status=?, api_response=?, message=?, verified_value=?,"
                    " attempts=attempts+1, updated_at=? WHERE id=?",
                    ("succeeded" if ok else "failed", api_status, json.dumps(result),
                     "" if ok else (message or verify_message or "write reported failure"), verified,
                     now(), item["id"]),
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

    @staticmethod
    def _count_statuses(db: DB, job_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in db.query("SELECT status FROM sync_items WHERE job_id=?", (job_id,)):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    @staticmethod
    def _classify(counts: dict[str, int]) -> str:
        """Job status from item outcomes — one rule, used by the run and by recovery."""
        total = sum(counts.values())
        failed = counts.get("failed", 0)
        return "failed" if failed and failed == total else ("partial" if failed else "completed")

    def _finish(self, job_id: int) -> None:
        counts = self._count_statuses(self.db, job_id)
        self.db.execute("UPDATE sync_jobs SET status=?, finished_at=?, summary_json=? WHERE id=?",
                        (self._classify(counts), now(), json.dumps(counts), job_id))

    def reconcile_interrupted_jobs(self) -> dict[str, int]:
        """Close jobs left half-run by a process that died mid-write.

        A live job executes inside the request that created it, so a crash between two items
        leaves the job ``in_progress`` with ``finished_at`` null forever, and the items that
        were never attempted sitting at ``ready`` — not failures, and invisible to ``retry()``,
        which read only failed and interrupted items. ``restart: unless-stopped`` makes that
        crash routine, not exotic.

        Marking both ``interrupted`` makes the half-run visible and re-runnable. It is safe
        rather than merely tidy: a write that had already landed is skipped as already applied
        on the retry, so re-running converges instead of duplicating entries in the PL tool.

        A job whose items all reached a terminal state before the crash is *not* called
        interrupted: nothing is left unwritten, so it is classified from its outcomes like any
        finished job. Labelling fully-applied work ``interrupted`` would send the reviewer
        looking for writes that are already in the PL tool, and offer a retry with nothing to
        retry.

        Called at startup, where nothing of ours can be mid-write. A job that is genuinely
        still running in *another* process would need a lease to be distinguishable; this
        single-process deployment has no lease, so it must not run two servers against the
        same database file.
        """
        with self.db.transaction():
            jobs = self.db.query(
                "SELECT id FROM sync_jobs WHERE status = 'in_progress'"
                " OR (status = 'pending' AND dry_run = 0)")
            recovered_items = 0
            for job in jobs:
                recovered_items += self.db.execute(
                    "UPDATE sync_items SET status='interrupted', updated_at=?"
                    " WHERE job_id=? AND status IN ('ready', 'pending')",
                    (now(), job["id"])).rowcount
                counts = self._count_statuses(self.db, job["id"])
                status = "interrupted" if counts.get("interrupted") else self._classify(counts)
                self.db.execute(
                    "UPDATE sync_jobs SET status=?, finished_at=?, summary_json=? WHERE id=?",
                    (status, now(), json.dumps(counts), job["id"]))
                self.db.audit("system", "sync.job_reconciled", "sync_job", job["id"],
                             {"status": status, "items": counts.get("interrupted", 0)}, now())
        return {"jobs": len(jobs), "items": recovered_items}

    def _jobs(self, job_ids: list[int]) -> list[dict[str, Any]]:
        """Any number of jobs in three reads, grouped per job in Python.

        ``job()`` used to run one query per sync item to name its component, so a job listing
        cost followed the item count instead of the job count.
        """
        if not job_ids:
            return []
        marks = ",".join("?" * len(job_ids))
        jobs = {j["id"]: j for j in self.db.query(f"SELECT * FROM sync_jobs WHERE id IN ({marks})", tuple(job_ids))}
        items = self.db.query(f"SELECT * FROM sync_items WHERE job_id IN ({marks}) ORDER BY id", tuple(job_ids))
        matches: dict[int, dict[str, Any]] = {}
        for chunk in _chunks(sorted({i["match_id"] for i in items})):
            chunk_marks = ",".join("?" * len(chunk))
            for row in self.db.query(
                f"SELECT id, pl_name, pl_version, sl_name, sl_version FROM matches WHERE id IN ({chunk_marks})",
                tuple(chunk)):
                matches[row["id"]] = row
        by_job: dict[int, list[dict[str, Any]]] = {jid: [] for jid in job_ids}
        for item in items:
            item["pl_before"] = json.loads(item["pl_before"]) if item["pl_before"] else None
            by_job[item["job_id"]].append(item)
        out: list[dict[str, Any]] = []
        for job_id in job_ids:
            job = jobs.get(job_id)
            if not job:
                continue
            by_component: dict[int, dict[str, Any]] = {}
            for item in by_job[job_id]:
                m = matches.get(item["match_id"]) or {}
                comp = by_component.setdefault(item["match_id"], {
                    "match_id": item["match_id"], "pl_name": m.get("pl_name"),
                    "pl_version": m.get("pl_version"), "items": [], "counts": {}})
                comp["items"].append(item)
                comp["counts"][item["status"]] = comp["counts"].get(item["status"], 0) + 1
            job["summary"] = json.loads(job.pop("summary_json") or "{}")
            job["dry_run"] = bool(job["dry_run"])
            job["items"] = by_job[job_id]
            job["components"] = list(by_component.values())
            out.append(job)
        return out

    def job(self, job_id: int) -> dict[str, Any]:
        jobs = self._jobs([job_id])
        if not jobs:
            raise KeyError(f"sync job {job_id} not found")
        return jobs[0]

    def list_jobs(self, mapping_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if mapping_id:
            rows = self.db.query("SELECT id FROM sync_jobs WHERE mapping_id=? ORDER BY id DESC LIMIT ?",
                                 (mapping_id, limit))
        else:
            rows = self.db.query("SELECT id FROM sync_jobs ORDER BY id DESC LIMIT ?", (limit,))
        return self._jobs([r["id"] for r in rows])

    def retry(self, job_id: int, *, confirm: bool = False, actor: str = "reviewer") -> dict[str, Any]:
        job = self.job(job_id)
        if job["dry_run"]:
            raise ValueError("dry-run jobs have nothing to retry; create a live job")
        if not confirm:
            raise ValueError("retry requires confirm=true")
        failed = [i for i in job["items"] if i["status"] in RETRYABLE_ITEM_STATUSES]
        if not failed:
            raise ValueError("no failed or interrupted items to retry")
        with self.db.transaction():
            new_id = self.db.execute(
                "INSERT INTO sync_jobs(mapping_id,dry_run,status,created_by,created_at) VALUES(?,0,'pending',?,?)",
                (job["mapping_id"], actor, now()),
            ).lastrowid
            for i in failed:
                self.db.execute(
                    "INSERT INTO sync_items(job_id,match_id,field,entry_key,status,pl_before,approved_value,"
                    "message,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (new_id, i["match_id"], i["field"], i["entry_key"], "ready", json.dumps(i["pl_before"]),
                     i["approved_value"], "retry of item %s" % i["id"], now()),
                )
            self.db.audit(actor, "sync.retry", "sync_job", new_id, {"from": job_id, "items": len(failed)}, now())
        mapping = self.s.mapping(job["mapping_id"])
        pl_conn = self.s.connector_for(mapping["pl_connection_id"])
        self._execute(new_id, mapping, pl_conn, actor)
        return self.job(new_id)
