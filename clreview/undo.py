"""Undo a live synchronization job from its stored before-values.

The audit snapshot is the sync job itself: every item keeps the PL value it saw
before the write. Undo replays those values. Where a connector declares no write
capability, or the before-value is not restorable, the result names the manual action.
"""
from __future__ import annotations

import json
from typing import Any

from .connectors.base import ConnectorError
from .connectors.http import UnsupportedOperation
from .domain import (
    FIELD_COPYRIGHT,
    FIELD_COPYRIGHT_DEACTIVATE,
    FIELD_LICENSE_NAME,
    FIELD_LICENSE_TEXT,
    FIELD_REVIEW_STATUS,
    FIELD_UPLOAD_STATUS,
)
from .service import now
from .sync import SyncService, _pl_component


class UndoService:
    def __init__(self, sync: SyncService):
        self.sync = sync
        self.s = sync.s
        self.db = sync.db

    def preview(self, job_id: int) -> dict[str, Any]:
        job = self.sync.job(job_id)
        if job["dry_run"]:
            raise ValueError("dry-run jobs have nothing to undo")
        mapping = self.s.mapping(job["mapping_id"])
        pl_conn = self.s.connector_for(mapping["pl_connection_id"])
        rows = []
        for item in job["items"]:
            match = self.db.one("SELECT * FROM matches WHERE id=?", (item["match_id"],))
            pl_row = _pl_component(self.s, match["mapping_id"], match["pl_key"]) if match else None
            if not pl_row or not pl_row.get("write_key"):
                rows.append({"item_id": item["id"], "field": item["field"], "possible": False,
                             "reason": "no writable PL handle"})
                continue
            caps = pl_conn.capabilities
            possible, reason = self._possible(item, caps)
            rows.append({"item_id": item["id"], "field": item["field"],
                         "before": item["pl_before"], "approved": item["approved_value"],
                         "possible": possible, "reason": reason})
        return {"job_id": job_id, "items": rows}

    def undo(self, job_id: int, confirm: bool = False, actor: str = "reviewer") -> dict[str, Any]:
        job = self.sync.job(job_id)
        if job["dry_run"]:
            raise ValueError("dry-run jobs have nothing to undo")
        if not confirm:
            raise ValueError("undo requires confirm=true")
        mapping = self.s.mapping(job["mapping_id"])
        pl_conn = self.s.connector_for(mapping["pl_connection_id"])
        sl_conn = self.s.connector_for(mapping["sl_connection_id"])
        undone: list[dict[str, Any]] = []
        manual: list[dict[str, Any]] = []
        for item in job["items"]:
            if item["status"] not in ("succeeded", "skipped_already_applied"):
                manual.append({"item_id": item["id"], "field": item["field"],
                               "reason": f"item status {item['status']}"})
                continue
            match = self.db.one("SELECT * FROM matches WHERE id=?", (item["match_id"],))
            pl_row = _pl_component(self.s, match["mapping_id"], match["pl_key"]) if match else None
            if not pl_row or not pl_row.get("write_key"):
                manual.append({"item_id": item["id"], "field": item["field"],
                               "reason": "no writable PL handle"})
                continue
            pl_key = pl_row["write_key"]
            caps = pl_conn.capabilities
            try:
                self._restore(item, match, pl_key, pl_conn, sl_conn, caps)
                undone.append({"item_id": item["id"], "field": item["field"]})
            except UnsupportedOperation as exc:
                manual.append({"item_id": item["id"], "field": item["field"], "reason": str(exc)})
            except ConnectorError as exc:
                manual.append({"item_id": item["id"], "field": item["field"], "reason": str(exc)})
        self.db.execute("UPDATE sync_jobs SET status='undone', finished_at=?, summary_json=? WHERE id=?",
                        (now(), json.dumps({"undone": len(undone), "manual": len(manual)}), job_id))
        self.db.audit(actor, "sync.undo", "sync_job", job_id,
                      {"undone": len(undone), "manual": len(manual)}, now())
        return {"ok": True, "job_id": job_id, "undone": undone, "manual": manual}

    @staticmethod
    def _possible(item: dict[str, Any], caps: Any) -> tuple[bool, str]:
        field = item["field"]
        if field in (FIELD_COPYRIGHT, FIELD_COPYRIGHT_DEACTIVATE) and not caps.write_copyrights:
            return False, "PL connector cannot write copyrights"
        if field == FIELD_LICENSE_NAME and not caps.write_license_names:
            return False, "PL connector cannot write license names"
        if field == FIELD_LICENSE_TEXT and not caps.write_license_text:
            return False, "PL connector cannot write license text"
        if field == FIELD_REVIEW_STATUS and not caps.write_review_status:
            return False, "PL connector cannot write review status"
        if field == FIELD_UPLOAD_STATUS:
            return True, ""
        return True, ""

    def _restore(self, item: dict[str, Any], match: dict[str, Any], pl_key: str,
                 pl_conn: Any, sl_conn: Any, caps: Any) -> None:
        field = item["field"]
        before = item["pl_before"]
        if field == FIELD_LICENSE_NAME:
            if not caps.write_license_names:
                raise UnsupportedOperation("PL connector cannot write license names")
            entries = [{"name": name} for name in (before or [])]
            pl_conn.update_licenses(pl_key, entries)
            return
        if field == FIELD_COPYRIGHT:
            if not caps.write_copyrights:
                raise UnsupportedOperation("PL connector cannot write copyrights")
            if item["approved_value"] in (before or []):
                return  # it was there before the sync; nothing was added
            if not hasattr(pl_conn, "set_copyright_active"):
                raise UnsupportedOperation("PL connector cannot remove an added notice")
            # Black Duck only creates statements, so rewriting the list cannot take one away:
            # the added notice is deactivated, the PL tool's own reversible switch
            pl_conn.set_copyright_active(pl_key, item["approved_value"], False)
            return
        if field == FIELD_COPYRIGHT_DEACTIVATE:
            if not caps.write_copyrights or not hasattr(pl_conn, "set_copyright_active"):
                raise UnsupportedOperation("PL connector cannot reactivate copyrights")
            pl_conn.set_copyright_active(pl_key, item["approved_value"], True)
            return
        if field == FIELD_LICENSE_TEXT:
            if not caps.write_license_text:
                raise UnsupportedOperation("PL connector cannot write license text")
            if not isinstance(before, dict) or not before.get("text"):
                raise UnsupportedOperation("the stored before-value has no license text to restore")
            pl_conn.update_license_text(pl_key, before["text"], before.get("file_name"))
            return
        if field == FIELD_REVIEW_STATUS:
            if not caps.write_review_status:
                raise UnsupportedOperation("PL connector cannot write review status")
            if before is None:
                raise UnsupportedOperation("the stored before-value has no review status")
            pl_conn.set_review_status(pl_key, before)
            return
        if field == FIELD_UPLOAD_STATUS:
            upload_id = item["entry_key"]
            if not upload_id:
                raise UnsupportedOperation("no FOSSology upload handle")
            current = sl_conn.upload_status(upload_id)
            if current == "Open":
                return
            sl_conn.close_upload(upload_id, "undo", "Open")
            return
        raise UnsupportedOperation(f"unknown field {field!r}")
