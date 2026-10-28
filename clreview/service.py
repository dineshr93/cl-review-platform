"""Application service: connections, project mappings, snapshot refresh, overview."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from .connectors.base import ConnectorError, create_connector, tool_catalog
from .db import DB
from .domain import ComponentData, sha256
from .matching import match_components

SECRET_KEYS = ("token", "password", "username", "api_key", "secret")

IN_CHUNK = 500  # stay well under SQLite's host-parameter limit when building IN (...) lists


def _chunks(items: list[Any]) -> list[list[Any]]:
    return [items[i:i + IN_CHUNK] for i in range(0, len(items), IN_CHUNK)]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_version(c: ComponentData) -> str:
    """Hash of a component's observations: detects stale approvals after refresh."""
    payload = {
        "copyrights": sorted(e.text for e in c.copyrights if e.active),
        "licenses": sorted(f"{e.name}|{e.spdx}|{e.license_type}" for e in c.licenses if e.active),
        "texts": sorted((e.content_hash or sha256(e.text)) + "|" + e.kind for e in c.license_texts),
    }
    return sha256(json.dumps(payload, sort_keys=True))


def sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if any(s in k.lower() for s in SECRET_KEYS) else v) for k, v in (config or {}).items()}


class Service:
    def __init__(self, db: DB, fixtures_dir: str | None = None):
        self.db = db
        self.fixtures_dir = fixtures_dir
        self._connector_cache: dict[tuple, Any] = {}

    # --- connections --------------------------------------------------------
    def add_connection(self, role: str, tool: str, name: str, base_url: str = "",
                       config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None) -> dict[str, Any]:
        if role not in ("pl", "sl"):
            raise ValueError("role must be 'pl' or 'sl'")
        cfg = dict(config or {})
        secrets = sorted(k for k in cfg if any(s in k.lower() for s in SECRET_KEYS))
        if secrets:
            raise ValueError(
                "connection config must not carry credentials: "
                f"{', '.join(secrets)}; name the environment variable in auth_env instead"
            )
        if self.fixtures_dir and tool.startswith("mock"):
            cfg.setdefault("fixtures", os.path.join(self.fixtures_dir, "pl.json" if role == "pl" else "sl.json"))
        create_connector(tool, role, base_url, cfg, auth_env or {})  # validates tool/role pair
        cur = self.db.execute(
            "INSERT INTO connections(role,tool,name,base_url,config_json,auth_env_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (role, tool, name, base_url, json.dumps(cfg), json.dumps(auth_env or {}), now()),
        )
        cid = cur.lastrowid
        self.db.audit("system", "connection.created", "connection", cid, {"role": role, "tool": tool, "name": name}, now())
        return self.connection(cid)

    def connection(self, connection_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM connections WHERE id=?", (connection_id,))
        if not row:
            raise KeyError(f"connection {connection_id} not found")
        row["config"] = sanitize_config(json.loads(row.pop("config_json")))
        row["auth_env"] = json.loads(row.pop("auth_env_json"))
        row["last_test_ok"] = bool(row["last_test_ok"])
        return row

    def list_connections(self) -> list[dict[str, Any]]:
        return [self.connection(r["id"]) for r in self.db.query("SELECT id FROM connections ORDER BY id")]

    def _raw_connection(self, connection_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM connections WHERE id=?", (connection_id,))
        if not row:
            raise KeyError(f"connection {connection_id} not found")
        row["config"] = json.loads(row.pop("config_json"))
        row["auth_env"] = json.loads(row.pop("auth_env_json"))
        return row

    def connector_for(self, connection_id: int) -> Any:
        row = self._raw_connection(connection_id)
        key = (row["id"], row["base_url"], json.dumps(row["config"], sort_keys=True), json.dumps(row["auth_env"], sort_keys=True))
        if key not in self._connector_cache:
            self._connector_cache[key] = create_connector(row["tool"], row["role"], row["base_url"] or "",
                                                          row["config"], row["auth_env"])
        return self._connector_cache[key]

    def test_connection(self, connection_id: int, actor: str = "reviewer") -> dict[str, Any]:
        try:
            result = self.connector_for(connection_id).test_connection()
            ok = bool(result.get("ok"))
            self.db.execute("UPDATE connections SET last_test_ok=?, last_test_at=?, last_test_error=? WHERE id=?",
                            (1 if ok else 0, now(), None, connection_id))
            self.db.audit(actor, "connection.tested", "connection", connection_id, {"ok": ok}, now())
            return {"ok": ok, "result": result}
        except Exception as exc:  # connector errors are surfaced, never swallowed as success
            msg = str(exc)
            self.db.execute("UPDATE connections SET last_test_ok=0, last_test_at=?, last_test_error=? WHERE id=?",
                            (now(), msg, connection_id))
            self.db.audit(actor, "connection.test_failed", "connection", connection_id, {"error": msg}, now())
            return {"ok": False, "error": msg}

    # --- discovery ----------------------------------------------------------
    def list_projects(self, connection_id: int) -> list[dict[str, Any]]:
        conn = self._raw_connection(connection_id)
        if conn["role"] != "pl":
            raise ValueError("project discovery requires a project-level connection")
        out = []
        for p in self.connector_for(connection_id).list_projects():
            versions = self.connector_for(connection_id).list_versions(p.external_id)
            out.append({"id": p.external_id, "name": p.name, "versions": versions})
        return out

    def list_scans(self, connection_id: int) -> list[dict[str, Any]]:
        conn = self._raw_connection(connection_id)
        if conn["role"] != "sl":
            raise ValueError("scan discovery requires a source-level connection")
        return [{"id": s.external_id, "name": s.name, "date": s.date, "description": s.description}
                for s in self.connector_for(connection_id).list_scans()]

    def list_folders(self, connection_id: int) -> list[dict[str, Any]]:
        """FOSSology folders, so a deploy target can be picked instead of typed as an id."""
        conn = self._raw_connection(connection_id)
        if conn["role"] != "sl":
            raise ValueError("folder discovery requires a source-level connection")
        connector = self.connector_for(connection_id)
        if not hasattr(connector, "list_folders"):
            raise ValueError(f"{conn['tool']} has no folder concept to deploy into")
        return [{"id": f.get("id"), "name": f.get("name"), "parent": f.get("parent")}
                for f in connector.list_folders()]

    def tools(self) -> list[dict[str, Any]]:
        return tool_catalog()

    # --- mappings -----------------------------------------------------------
    def create_mapping(self, name: str, pl_connection_id: int, pl_project: str, pl_version: str | None,
                       sl_connection_id: int, sl_scan: str, actor: str = "reviewer") -> dict[str, Any]:
        pl = self._raw_connection(pl_connection_id)
        sl = self._raw_connection(sl_connection_id)
        if pl["role"] != "pl" or sl["role"] != "sl":
            raise ValueError("mapping requires one PL connection and one SL connection")
        cur = self.db.execute(
            "INSERT INTO mappings(name,pl_connection_id,sl_connection_id,pl_project,pl_version,sl_scan,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (name, pl_connection_id, sl_connection_id, pl_project, pl_version, sl_scan, now()),
        )
        mid = cur.lastrowid
        self.db.audit(actor, "mapping.created", "mapping", mid,
                      {"pl": pl_project, "pl_version": pl_version, "sl": sl_scan}, now())
        return self.mapping(mid)

    def mapping(self, mapping_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM mappings WHERE id=?", (mapping_id,))
        if not row:
            raise KeyError(f"mapping {mapping_id} not found")
        row["pl_connection"] = self.connection(row["pl_connection_id"])
        row["sl_connection"] = self.connection(row["sl_connection_id"])
        return row

    def list_mappings(self) -> list[dict[str, Any]]:
        return [self.mapping(r["id"]) for r in self.db.query("SELECT id FROM mappings ORDER BY id")]

    # --- snapshots ----------------------------------------------------------
    def current_snapshot(self, mapping_id: int, side: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT * FROM snapshots WHERE mapping_id=? AND side=? AND status='ok' ORDER BY id DESC LIMIT 1",
            (mapping_id, side),
        )

    def snapshot_components(self, snapshot_id: int) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM components WHERE snapshot_id=? ORDER BY name, version", (snapshot_id,))

    def component_by_key(self, mapping_id: int, side: str, key: str) -> dict[str, Any] | None:
        snap = self.current_snapshot(mapping_id, side)
        if not snap:
            return None
        return self.db.one("SELECT * FROM components WHERE snapshot_id=? AND external_id=?", (snap["id"], key))

    def component_observations(self, component_id: int) -> dict[str, Any]:
        return {
            "copyrights": self.db.query("SELECT * FROM copyrights WHERE component_id=? ORDER BY id", (component_id,)),
            "licenses": self.db.query("SELECT * FROM licenses WHERE component_id=? ORDER BY id", (component_id,)),
            "license_texts": self.db.query("SELECT * FROM license_texts WHERE component_id=? ORDER BY id", (component_id,)),
        }

    def components_observations(self, snapshot_id: int) -> dict[int, dict[str, list[dict[str, Any]]]]:
        """Every observation row of a snapshot in one query per table, keyed by component id."""
        ids = [r["id"] for r in self.db.query("SELECT id FROM components WHERE snapshot_id=?", (snapshot_id,))]
        out: dict[int, dict[str, list[dict[str, Any]]]] = {
            i: {"copyrights": [], "licenses": [], "license_texts": []} for i in ids}
        for table in ("copyrights", "licenses", "license_texts"):
            for chunk in _chunks(ids):
                marks = ",".join("?" * len(chunk))
                rows = self.db.query(f"SELECT * FROM {table} WHERE component_id IN ({marks}) ORDER BY id", tuple(chunk))
                for row in rows:
                    out[row["component_id"]][table].append(row)
        return out

    def refresh(self, mapping_id: int, actor: str = "reviewer") -> dict[str, Any]:
        m = self.mapping(mapping_id)
        sides = {
            "pl": (m["pl_connection_id"], (m["pl_project"], m["pl_version"] or "")),
            "sl": (m["sl_connection_id"], (m["sl_scan"],)),
        }
        results: dict[str, Any] = {}
        for side, (cid, args) in sides.items():
            conn = self.connector_for(cid)
            try:
                inventory = conn.fetch_inventory(*args)
                sid = self._store_snapshot(mapping_id, side, conn.tool, inventory)
                results[side] = {"status": "ok", "count": len(inventory), "snapshot_id": sid, "retrieved_at": now()}
            except ConnectorError as exc:
                sid = self.db.execute(
                    "INSERT INTO snapshots(mapping_id,side,tool,retrieved_at,status,error) VALUES(?,?,?,?,?,?)",
                    (mapping_id, side, conn.tool, now(), "error", str(exc)),
                ).lastrowid
                results[side] = {"status": "error", "error": str(exc), "snapshot_id": sid}
        matching = self._run_matching(mapping_id)
        self.db.audit(actor, "snapshot.refreshed", "mapping", mapping_id,
                      {"sides": {k: v.get("status") for k, v in results.items()}, "matching": matching}, now())
        return {"mapping_id": mapping_id, "sides": results, "matching": matching}

    def _store_snapshot(self, mapping_id: int, side: str, tool: str, inventory: list[ComponentData]) -> int:
        """Store one side's inventory as one unit: a half-stored snapshot would be matched."""
        version_hash = sha256(json.dumps(sorted(source_version(c) for c in inventory), sort_keys=True))
        with self.db.transaction():
            return self._store_snapshot_rows(mapping_id, side, tool, inventory, version_hash)

    def _store_snapshot_rows(self, mapping_id: int, side: str, tool: str, inventory: list[ComponentData],
                             version_hash: str) -> int:
        sid = self.db.execute(
            "INSERT INTO snapshots(mapping_id,side,tool,retrieved_at,status,item_count,version_hash)"
            " VALUES(?,?,?,?,?,?,?)",
            (mapping_id, side, tool, now(), "ok", len(inventory), version_hash),
        ).lastrowid
        for c in inventory:
            cid = self.db.execute(
                "INSERT INTO components(snapshot_id,side,tool,external_id,name,version,purl,metadata_json,raw_json,source_version,write_key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, side, tool, c.external_id, c.name, c.version, c.purl,
                 json.dumps(c.metadata), json.dumps(c.raw), source_version(c), c.write_key),
            ).lastrowid
            for e in c.copyrights:
                self.db.execute(
                    "INSERT INTO copyrights(component_id,text,normalized,hash,origin,source_path,active,retrieved_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (cid, e.text, e.text.strip().lower(), sha256(e.text), e.origin, e.source_path,
                     1 if e.active else 0, now()),
                )
            for e in c.licenses:
                self.db.execute(
                    "INSERT INTO licenses(component_id,name,spdx,license_type,evidence_paths_json,active,source,retrieved_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (cid, e.name, e.spdx, e.license_type, json.dumps(e.evidence_paths), 1 if e.active else 0,
                     e.source, now()),
                )
            for e in c.license_texts:
                self.db.execute(
                    "INSERT INTO license_texts(component_id,kind,file_name,file_path,text,content_hash,provenance,is_license_file,retrieved_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (cid, e.kind, e.file_name, e.file_path, e.text, e.content_hash or sha256(e.text),
                     e.provenance, 1 if e.is_license_file else 0, now()),
                )
        return sid

    def side_components_with_observations(self, mapping_id: int, side: str) -> dict[str, dict[str, Any]]:
        """{external_id: component + observations} for a side, in four queries instead of 4N."""
        snap = self.current_snapshot(mapping_id, side)
        if not snap:
            return {}
        observations = self.components_observations(snap["id"])
        return {row["external_id"]: {"component": row, **observations[row["id"]]}
                for row in self.snapshot_components(snap["id"])}

    @staticmethod
    def _to_component_data(entry: dict[str, Any]) -> ComponentData:
        row = entry["component"]
        return ComponentData(
            external_id=row["external_id"], name=row["name"], version=row["version"], purl=row["purl"],
            metadata=json.loads(row["metadata_json"]), raw=json.loads(row["raw_json"]),
            copyrights=[_ce(o) for o in entry["copyrights"]],
            licenses=[_le(o) for o in entry["licenses"]],
            license_texts=[_te(o) for o in entry["license_texts"]],
        )

    def _inventory_for_matching(self, mapping_id: int, side: str) -> list[ComponentData]:
        return [self._to_component_data(e)
                for e in self.side_components_with_observations(mapping_id, side).values()]

    def snapshot_versions(self, mapping_id: int, side: str, previous: bool = False) -> dict[str, str]:
        """{external_id: source_version} for this side's latest (or previous) successful snapshot."""
        snap = self.db.one(
            "SELECT id FROM snapshots WHERE mapping_id=? AND side=? AND status='ok' ORDER BY id DESC LIMIT 1 OFFSET ?",
            (mapping_id, side, 1 if previous else 0))
        if not snap:
            return {}
        rows = self.db.query("SELECT external_id, source_version FROM components WHERE snapshot_id=?", (snap["id"],))
        return {r["external_id"]: r["source_version"] for r in rows}

    def _run_matching(self, mapping_id: int) -> dict[str, Any]:
        pl_snap = self.current_snapshot(mapping_id, "pl")
        sl_snap = self.current_snapshot(mapping_id, "sl")
        sides = {side: self.side_components_with_observations(mapping_id, side) for side in ("pl", "sl")}
        pl = [self._to_component_data(e) for e in sides["pl"].values()]
        sl = [self._to_component_data(e) for e in sides["sl"].values()]
        if not pl_snap or not sl_snap:
            return {"status": "partial", "pl": len(pl), "sl": len(sl), "reason": "one side has no successful snapshot"}
        results = match_components(pl, sl)
        existing = {r["pl_key"] + "\x00" + r["sl_key"]: r for r in
                    self.db.query("SELECT * FROM matches WHERE mapping_id=?", (mapping_id,))}
        # A match only needs recomputing when its row moved or the observations behind it did.
        versions = {side: (self.snapshot_versions(mapping_id, side),
                           self.snapshot_versions(mapping_id, side, previous=True)) for side in ("pl", "sl")}
        added = 0
        changed: list[int] = []
        for r in results:
            pair = r.pl_key + "\x00" + r.sl_key
            row = existing.get(pair)
            if row:
                moved = (row["pl_name"], row["pl_version"], row["sl_name"], row["sl_version"]) != (
                    r.pl_name, r.pl_version, r.sl_name, r.sl_version)
                if not moved:
                    for side, key in (("pl", r.pl_key), ("sl", r.sl_key)):
                        if key and versions[side][0].get(key) != versions[side][1].get(key):
                            moved = True
                            break
                if not moved:
                    continue
                self.db.execute(
                    "UPDATE matches SET pl_name=?, pl_version=?, sl_name=?, sl_version=?, updated_at=? WHERE id=?",
                    (r.pl_name, r.pl_version, r.sl_name, r.sl_version, now(), row["id"]),
                )
                changed.append(row["id"])
            else:
                cur = self.db.execute(
                    "INSERT INTO matches(mapping_id,pl_key,sl_key,method,confidence,status,rationale_json,mismatch_json,"
                    "pl_name,pl_version,sl_name,sl_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mapping_id, r.pl_key, r.sl_key, r.method, r.confidence, r.status,
                     json.dumps(r.rationale), json.dumps(r.mismatch_reasons), r.pl_name, r.pl_version,
                     r.sl_name, r.sl_version, now(), now()),
                )
                added += 1
                changed.append(cur.lastrowid)
        from .review import RecomputeContext, ReviewService, decisions_by_match  # deferred: module cycle
        if changed:
            review = ReviewService(self)
            rows = {r["id"]: r for r in self.db.query("SELECT * FROM matches WHERE mapping_id=?", (mapping_id,))}
            decisions = decisions_by_match(self.db, changed)
            for match_id in changed:
                review.recompute(match_id, context=RecomputeContext(
                    match=rows[match_id], decisions=decisions.get(match_id, []), inventory=sides))
        return {"status": "ok", "pl": len(pl), "sl": len(sl), "proposed": len(results), "new_matches": added}

    # --- overview
    def overview(self, mapping_id: int) -> dict[str, Any]:
        m = self.mapping(mapping_id)
        sides = {}
        for side in ("pl", "sl"):
            snap = self.current_snapshot(mapping_id, side)
            last = self.db.one(
                "SELECT * FROM snapshots WHERE mapping_id=? AND side=? ORDER BY id DESC LIMIT 1", (mapping_id, side))
            sides[side] = {
                "snapshot_id": snap["id"] if snap else None,
                "retrieved_at": snap["retrieved_at"] if snap else None,
                "count": snap["item_count"] if snap else 0,
                "status": last["status"] if last else "never",
                "error": last["error"] if last else None,
                "tool": last["tool"] if last else None,
            }
        matches = self._current_matches(mapping_id)
        counts = {
            "matched": sum(1 for r in matches if r["sl_key"] and r["pl_key"]),
            "suggested": sum(1 for r in matches if r["status"] == "suggested"),
            "unmatched": sum(1 for r in matches if r["status"] == "unmatched"),
            "pending_review": sum(1 for r in matches if r["review_status"] in ("review_required", "in_progress")),
            "approved": sum(1 for r in matches if r["review_status"] == "approved_for_sync"),
            "conflicts": sum(1 for r in matches if r.get("stale")),
        }
        recent = self.db.query(
            "SELECT id,dry_run,status,created_by,created_at,finished_at,summary_json FROM sync_jobs"
            " WHERE mapping_id=? ORDER BY id DESC LIMIT 5", (mapping_id,))
        return {"mapping": m, "sides": sides, "counts": counts, "recent_sync": recent}

    def _current_matches(self, mapping_id: int) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM matches WHERE mapping_id=? ORDER BY id", (mapping_id,))
        pl_snap = self.current_snapshot(mapping_id, "pl")
        sl_snap = self.current_snapshot(mapping_id, "sl")
        pl_keys = {r["external_id"] for r in self.snapshot_components(pl_snap["id"])} if pl_snap else set()
        sl_keys = {r["external_id"] for r in self.snapshot_components(sl_snap["id"])} if sl_snap else set()
        out = []
        for r in rows:
            r["pl_present"] = bool(r["pl_key"] and r["pl_key"] in pl_keys)
            r["sl_present"] = bool(r["sl_key"] and r["sl_key"] in sl_keys)
            if r["status"] == "unmatched" and r["pl_key"] and not r["pl_present"]:
                continue
            if r["status"] == "unmatched" and r["sl_key"] and not r["sl_present"]:
                continue
            if r["status"] != "unmatched" and not (r["pl_present"] and r["sl_present"]) and r["status"] != "confirmed":
                r["stale"] = True
            out.append(r)
        return out

    def audit_events(self, mapping_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if mapping_id is None:
            rows = self.db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.db.query(
                "SELECT * FROM audit WHERE (entity_type='mapping' AND entity_id=?)"
                " OR (entity_type IN ('match','sync_item') AND entity_id IN (SELECT CAST(id AS TEXT) FROM matches WHERE mapping_id=?))"
                " OR (entity_type='sync_job' AND entity_id IN (SELECT CAST(id AS TEXT) FROM sync_jobs WHERE mapping_id=?))"
                " ORDER BY id DESC LIMIT ?", (str(mapping_id), mapping_id, mapping_id, limit))
        for r in rows:
            r["detail"] = json.loads(r.pop("detail_json"))
        return rows


# --- row -> domain helpers ---------------------------------------------------

from .domain import CopyrightEntry, LicenseEntry, LicenseTextCandidate  # noqa: E402


def _ce(row: dict[str, Any]) -> CopyrightEntry:
    return CopyrightEntry(text=row["text"], origin=row["origin"], source_path=row["source_path"],
                          active=bool(row["active"]), retrieved_at=row["retrieved_at"])


def _le(row: dict[str, Any]) -> LicenseEntry:
    return LicenseEntry(name=row["name"], spdx=row["spdx"], license_type=row["license_type"],
                        evidence_paths=json.loads(row["evidence_paths_json"] or "[]"),
                        active=bool(row["active"]), source=row["source"])


def _te(row: dict[str, Any]) -> LicenseTextCandidate:
    return LicenseTextCandidate(kind=row["kind"], text=row["text"], provenance=row["provenance"],
                                file_name=row["file_name"], file_path=row["file_path"],
                                content_hash=row["content_hash"], is_license_file=bool(row["is_license_file"]))
