"""Application service: connections, project mappings, snapshot refresh, overview."""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable

from .connectors.base import ConnectorError, RefreshCancelled, create_connector, tool_catalog
from .db import DB
from .connectors.http import get_json
from .domain import ComponentData, CopyrightEntry, LicenseEntry, LicenseTextCandidate, norm_license, sha256
from .license_catalog import SPDX_URL, parse_spdx, propose
from .matching import MatchResult, SLScope, match_components

SECRET_KEYS = ("token", "password", "username", "api_key", "secret")

IN_CHUNK = 500  # stay well under SQLite's host-parameter limit when building IN (...) lists
SNAPSHOTS_PER_SIDE = 5  # incremental matching needs the previous snapshot; a reviewer needs recent history
LONG_REFRESH_SECONDS = 30  # a refresh estimated at this or longer (or not estimable) asks the reviewer first
REFRESH_HISTORY = 20  # recent refresh audits searched for each side's last timing
TARGET_WAIT_SECONDS = 5  # a display-name lookup answers within this; a slower side shows its id for now
TARGET_RETRY_SECONDS = 60  # a tool that failed a lookup is not asked again for this long


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
        self._target_cache: dict[tuple, dict[str, str]] = {}
        self._spdx: list[dict[str, Any]] | None = None
        self._target_lock = threading.Lock()
        self._target_running: dict[tuple, Future] = {}
        self._target_failed: dict[tuple, float] = {}
        self._target_pool: ThreadPoolExecutor | None = None

    # --- connections --------------------------------------------------------
    def _check_connection_input(self, role: str, tool: str, base_url: str, config: dict[str, Any],
                                auth_env: dict[str, str]) -> dict[str, Any]:
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
        return cfg

    def add_connection(self, role: str, tool: str, name: str, base_url: str = "",
                       config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None) -> dict[str, Any]:
        if role not in ("pl", "sl"):
            raise ValueError("role must be 'pl' or 'sl'")
        cfg = self._check_connection_input(role, tool, base_url, config or {}, auth_env or {})
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO connections(role,tool,name,base_url,config_json,auth_env_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (role, tool, name, base_url, json.dumps(cfg), json.dumps(auth_env or {}), now()),
            )
            cid = cur.lastrowid
            self.db.audit("system", "connection.created", "connection", cid,
                          {"role": role, "tool": tool, "name": name}, now())
        return self.connection(cid)

    def update_connection(self, connection_id: int, name: str, base_url: str = "",
                          config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None,
                          actor: str = "reviewer") -> dict[str, Any]:
        """Edit a connection in place. Role and tool are fixed: mappings and snapshots depend on them."""
        row = self._raw_connection(connection_id)
        cfg = self._check_connection_input(row["role"], row["tool"], base_url, config or {}, auth_env or {})
        with self.db.transaction():
            self.db.execute(
                "UPDATE connections SET name=?, base_url=?, config_json=?, auth_env_json=?,"
                " last_test_ok=0, last_test_at=NULL, last_test_error=NULL WHERE id=?",
                (name or row["tool"], base_url, json.dumps(cfg), json.dumps(auth_env or {}), connection_id),
            )
            self.db.audit(actor, "connection.updated", "connection", connection_id,
                          {"name": name, "base_url": base_url}, now())
        return self.connection(connection_id)

    def delete_connection(self, connection_id: int, actor: str = "reviewer") -> dict[str, Any]:
        row = self._raw_connection(connection_id)
        used = self.db.query(
            "SELECT name FROM mappings WHERE pl_connection_id=? OR sl_connection_id=? ORDER BY id",
            (connection_id, connection_id))
        if used:
            raise ValueError("connection is used by mapping(s): " + ", ".join(m["name"] for m in used)
                             + "; delete or repoint those first")
        with self.db.transaction():
            self.db.execute("DELETE FROM connections WHERE id=?", (connection_id,))
            self.db.audit(actor, "connection.deleted", "connection", connection_id,
                          {"role": row["role"], "tool": row["tool"], "name": row["name"]}, now())
        return {"deleted": connection_id}

    @staticmethod
    def _shape_connection(row: dict[str, Any]) -> dict[str, Any]:
        row["config"] = sanitize_config(json.loads(row.pop("config_json")))
        row["auth_env"] = json.loads(row.pop("auth_env_json"))
        row["last_test_ok"] = bool(row["last_test_ok"])
        return row

    def connection(self, connection_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM connections WHERE id=?", (connection_id,))
        if not row:
            raise KeyError(f"connection {connection_id} not found")
        return self._shape_connection(row)

    def list_connections(self) -> list[dict[str, Any]]:
        return [self._shape_connection(r) for r in self.db.query("SELECT * FROM connections ORDER BY id")]

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
            with self.db.transaction():
                self.db.execute("UPDATE connections SET last_test_ok=?, last_test_at=?, last_test_error=? WHERE id=?",
                                (1 if ok else 0, now(), None, connection_id))
                self.db.audit(actor, "connection.tested", "connection", connection_id, {"ok": ok}, now())
            return {"ok": ok, "result": result}
        except Exception as exc:  # connector errors are surfaced, never swallowed as success
            msg = str(exc)
            with self.db.transaction():
                self.db.execute("UPDATE connections SET last_test_ok=0, last_test_at=?, last_test_error=? WHERE id=?",
                                (now(), msg, connection_id))
                self.db.audit(actor, "connection.test_failed", "connection", connection_id, {"error": msg}, now())
            return {"ok": False, "error": msg}

    # --- discovery ----------------------------------------------------------
    def list_projects(self, connection_id: int, with_versions: bool = True) -> list[dict[str, Any]]:
        conn = self._raw_connection(connection_id)
        if conn["role"] != "pl":
            raise ValueError("project discovery requires a project-level connection")
        connector = self.connector_for(connection_id)
        return [{"id": p.external_id, "name": p.name,
                 "versions": connector.list_versions(p.external_id) if with_versions else []}
                for p in connector.list_projects()]

    def list_versions(self, connection_id: int, project_id: str) -> list[str]:
        conn = self._raw_connection(connection_id)
        if conn["role"] != "pl":
            raise ValueError("project discovery requires a project-level connection")
        return self.connector_for(connection_id).list_versions(project_id)

    def list_scans(self, connection_id: int, folders_only: bool = False) -> list[dict[str, Any]]:
        conn = self._raw_connection(connection_id)
        if conn["role"] != "sl":
            raise ValueError("scan discovery requires a source-level connection")
        connector = self.connector_for(connection_id)
        lister = getattr(connector, "list_folder_scans", None) if folders_only else None
        return [{"id": s.external_id, "name": s.name, "date": s.date, "description": s.description}
                for s in (lister or connector.list_scans)()]

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

    # --- license map -------------------------------------------------------
    def license_map_rows(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM license_map ORDER BY short_name")

    def license_map_lookup(self) -> dict[str, dict[str, Any]]:
        return {norm_license(r["short_name"]): r for r in self.license_map_rows()}

    def upsert_license_map(self, short_name: str, spdx: str | None, bd_license: str,
                           actor: str = "reviewer", source: str = "manual") -> dict[str, Any]:
        if not norm_license(short_name):
            raise ValueError("license short name is required")
        if not bd_license.strip():
            raise ValueError("bd_license is required")
        with self.db.transaction():
            row = self._put_license_map(short_name, spdx, bd_license, source)
            self.db.audit(actor, "license_map.upsert", "license_map", row["id"],
                          {"short_name": row["short_name"], "spdx": spdx, "bd_license": bd_license,
                           "source": source}, now())
        return row

    def _put_license_map(self, short_name: str, spdx: str | None, bd_license: str, source: str) -> dict[str, Any]:
        # The lookup is case-insensitive, so an existing row in any case is the row to update:
        # inserting beside it would leave two entries for one name and the older could win.
        existing = self.license_map_lookup().get(norm_license(short_name))
        values = ((spdx or "").strip() or None, bd_license.strip(), source, now())
        if existing:
            self.db.execute("UPDATE license_map SET spdx=?, bd_license=?, source=?, updated_at=? WHERE id=?",
                            (*values, existing["id"]))
            return self.db.one("SELECT * FROM license_map WHERE id=?", (existing["id"],))
        cur = self.db.execute(
            "INSERT INTO license_map(short_name,spdx,bd_license,source,updated_at) VALUES(?,?,?,?,?)",
            (short_name.strip(), *values))
        return self.db.one("SELECT * FROM license_map WHERE id=?", (cur.lastrowid,))

    def _spdx_licenses(self) -> list[dict[str, Any]]:
        """The official SPDX list, read once per process; ``CL_SPDX_URL`` may name a URL or a local file."""
        if self._spdx is None:
            src = os.environ.get("CL_SPDX_URL", SPDX_URL)
            if src.startswith(("http://", "https://")):
                data = get_json(src, timeout=20, retries=1)
            else:
                with open(src, encoding="utf-8") as fh:
                    data = json.load(fh)
            self._spdx = parse_spdx(data)
        return self._spdx

    def suggest_license_map(self) -> dict[str, Any]:
        """Proposed map entries from the SL names in snapshots, the PL catalogue and the SPDX list.

        Reads only: the reviewer applies what they accept. A source that cannot be read is a
        warning, not a failure, and the proposals say which sources they were built from.
        """
        def names(side: str) -> list[dict[str, Any]]:
            rows = self.db.query(
                "SELECT l.name, MAX(l.spdx) AS spdx FROM licenses l JOIN components c ON c.id=l.component_id"
                " WHERE c.side=? AND l.active=1 GROUP BY l.name ORDER BY l.name", (side,))
            return [{"name": r["name"], "spdx": r["spdx"]} for r in rows]

        warnings: list[str] = []
        sl = names("sl")
        pl = {norm_license(r["name"]): {**r, "in_use": True} for r in names("pl")}
        conns = self.db.query("SELECT DISTINCT pl_connection_id AS pl, sl_connection_id AS sl FROM mappings")
        for cid in sorted({c["pl"] for c in conns}):
            connector = self.connector_for(cid)
            if not hasattr(connector, "license_catalog"):
                continue
            try:
                for item in connector.license_catalog():
                    pl.setdefault(norm_license(item["name"]), {**item, "in_use": False})
            except Exception as exc:  # one tool down still leaves the others to match against
                warnings.append(f"PL license catalogue of connection {cid} unavailable: {exc}")
        for cid in sorted({c["sl"] for c in conns}):
            connector = self.connector_for(cid)
            if not hasattr(connector, "license_info"):
                continue
            try:
                info = connector.license_info([n["name"] for n in sl])
            except Exception as exc:
                warnings.append(f"SL license details of connection {cid} unavailable: {exc}")
                continue
            for n in sl:
                extra = info.get(n["name"]) or {}
                n.setdefault("full_name", extra.get("full_name"))
                n["spdx"] = n["spdx"] or extra.get("spdx")
        try:
            spdx = self._spdx_licenses()
        except Exception as exc:
            spdx = []
            warnings.append(f"SPDX license list unavailable ({exc}); matched on names only")
        result = propose(sl, list(pl.values()), spdx, self.license_map_lookup())
        result["sources"] = {"sl_names": len(sl), "pl_licenses": len(pl), "spdx_licenses": len(spdx)}
        result["warnings"] = warnings
        return result

    def apply_license_map(self, items: list[dict[str, Any]], actor: str = "reviewer") -> dict[str, Any]:
        """Write the accepted proposals in one transaction; all of them or none."""
        clean = []
        for item in items or []:
            short, bd = str(item.get("short_name") or "").strip(), str(item.get("bd_license") or "").strip()
            if not short or not bd:
                raise ValueError("every entry needs short_name and bd_license")
            clean.append((short, item.get("spdx"), bd))
        if not clean:
            raise ValueError("no entries to apply")
        with self.db.transaction():
            rows = [self._put_license_map(short, spdx, bd, "suggested") for short, spdx, bd in clean]
            self.db.audit(actor, "license_map.apply_suggestions", "license_map", "",
                          {"entries": [{"short_name": r["short_name"], "bd_license": r["bd_license"]} for r in rows]},
                          now())
        return {"applied": len(rows), "items": rows}

    def delete_license_map(self, map_id: int, actor: str = "reviewer") -> dict[str, Any]:
        row = self.db.one("SELECT * FROM license_map WHERE id=?", (map_id,))
        if not row:
            raise KeyError(f"license map {map_id} not found")
        with self.db.transaction():
            self.db.execute("DELETE FROM license_map WHERE id=?", (map_id,))
            self.db.audit(actor, "license_map.delete", "license_map", map_id,
                          {"short_name": row["short_name"]}, now())
        return {"ok": True, "deleted": map_id}

    def connection_capabilities(self, connection_id: int) -> dict[str, Any]:
        conn = self.connector_for(connection_id)
        caps = conn.capabilities
        return {
            "read_components": caps.read_components,
            "read_copyrights": caps.read_copyrights,
            "read_license_text": caps.read_license_text,
            "write_copyrights": caps.write_copyrights,
            "write_license_names": caps.write_license_names,
            "write_license_text": caps.write_license_text,
            "write_review_status": caps.write_review_status,
            "write_upload_status": caps.write_upload_status,
            "verify_writes": caps.verify_writes,
            "notes": caps.notes,
        }

    # --- mappings -----------------------------------------------------------
    def create_mapping(self, name: str, pl_connection_id: int, pl_project: str, pl_version: str | None,
                       sl_connection_id: int, sl_scan: str, actor: str = "reviewer") -> dict[str, Any]:
        pl = self._raw_connection(pl_connection_id)
        sl = self._raw_connection(sl_connection_id)
        if pl["role"] != "pl" or sl["role"] != "sl":
            raise ValueError("mapping requires one PL connection and one SL connection")
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO mappings(name,pl_connection_id,sl_connection_id,pl_project,pl_version,sl_scan,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (name, pl_connection_id, sl_connection_id, pl_project, pl_version, sl_scan, now()),
            )
            mid = cur.lastrowid
            self.db.audit(actor, "mapping.created", "mapping", mid,
                          {"pl": pl_project, "pl_version": pl_version, "sl": sl_scan}, now())
        return self.mapping(mid)

    def _shape_mapping(self, row: dict[str, Any], connections: dict[int, dict[str, Any]]) -> dict[str, Any]:
        row["pl_connection"] = connections[row["pl_connection_id"]]
        row["sl_connection"] = connections[row["sl_connection_id"]]
        return row

    def mapping(self, mapping_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM mappings WHERE id=?", (mapping_id,))
        if not row:
            raise KeyError(f"mapping {mapping_id} not found")
        return self._shape_mapping(row, {c["id"]: c for c in self.list_connections()})

    def mapping_targets(self, mapping_id: int) -> dict[str, dict[str, str] | None]:
        """Display name and tool link for each side of a mapping; ``None`` where it cannot be resolved.

        Resolution reads the tool live, so it is a separate call from ``mapping()`` and a failure
        (tool down, id deleted) leaves that side to be shown by its stored id.

        Both sides are read in parallel and the answer comes within ``TARGET_WAIT_SECONDS``: a side
        still reading shows its id now and is cached for the next call when it lands. A failure is
        remembered for ``TARGET_RETRY_SECONDS``, so a tool that is down is not asked on every page.
        """
        row = self.mapping(mapping_id)
        wanted = {"pl": (row["pl_connection_id"], (row["pl_project"], row["pl_version"] or "")),
                  "sl": (row["sl_connection_id"], (row["sl_scan"],))}
        out: dict[str, dict[str, str] | None] = {}
        pending: dict[str, Future] = {}
        with self._target_lock:
            for side, (connection_id, args) in wanted.items():
                key = (connection_id, args)
                if key in self._target_cache:
                    out[side] = self._target_cache[key]
                elif time.monotonic() - self._target_failed.get(key, float("-inf")) < TARGET_RETRY_SECONDS:
                    out[side] = None
                else:
                    if key not in self._target_running:
                        if self._target_pool is None:
                            self._target_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="targets")
                        self._target_running[key] = self._target_pool.submit(self._describe_target, key)
                    pending[side] = self._target_running[key]
        done, _ = wait(pending.values(), timeout=TARGET_WAIT_SECONDS)
        for side, fut in pending.items():
            out[side] = fut.result() if fut in done else None
        return out

    def _describe_target(self, key: tuple) -> dict[str, str] | None:
        connection_id, args = key
        try:
            describe = getattr(self.connector_for(connection_id), "describe_target", None)
            result = describe(*args) if describe else None
            with self._target_lock:
                if result is not None:
                    self._target_cache[key] = result
                self._target_failed.pop(key, None)
            return result
        except Exception:
            with self._target_lock:
                self._target_failed[key] = time.monotonic()
            return None
        finally:
            with self._target_lock:
                self._target_running.pop(key, None)

    def list_mappings(self) -> list[dict[str, Any]]:
        connections = {c["id"]: c for c in self.list_connections()}
        return [self._shape_mapping(r, connections)
                for r in self.db.query("SELECT * FROM mappings ORDER BY id")]

    # Children first, explicitly: databases created before the FK clauses existed have no cascade.
    _MAPPING_ROWS = (
        ("license_texts", "component_id IN (SELECT c.id FROM components c JOIN snapshots s ON s.id=c.snapshot_id WHERE s.mapping_id=?)"),
        ("licenses", "component_id IN (SELECT c.id FROM components c JOIN snapshots s ON s.id=c.snapshot_id WHERE s.mapping_id=?)"),
        ("copyrights", "component_id IN (SELECT c.id FROM components c JOIN snapshots s ON s.id=c.snapshot_id WHERE s.mapping_id=?)"),
        ("components", "snapshot_id IN (SELECT id FROM snapshots WHERE mapping_id=?)"),
        ("pl_listing", "snapshot_id IN (SELECT id FROM snapshots WHERE mapping_id=?)"),
        ("sl_license_files", "mapping_id=?"),
        ("snapshots", "mapping_id=?"),
        ("sync_items", "job_id IN (SELECT id FROM sync_jobs WHERE mapping_id=?)"),
        ("sync_jobs", "mapping_id=?"),
        ("decisions", "match_id IN (SELECT id FROM matches WHERE mapping_id=?)"),
        ("notes", "match_id IN (SELECT id FROM matches WHERE mapping_id=?)"),
        ("suggestions", "match_id IN (SELECT id FROM matches WHERE mapping_id=?)"),
        ("false_positive_rules", "mapping_id=? AND scope != 'global'"),
        ("matches", "mapping_id=?"),
    )

    def mapping_usage(self, mapping_id: int) -> dict[str, Any]:
        """How many rows a delete of this mapping would remove, per table."""
        m = self.mapping(mapping_id)
        counts = {table: self.db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", (mapping_id,))["n"]
                  for table, where in self._MAPPING_ROWS}
        return {"mapping": {"id": m["id"], "name": m["name"]}, "counts": counts}

    def delete_mapping(self, mapping_id: int, confirm: str, actor: str = "reviewer") -> dict[str, Any]:
        """Remove the mapping and every row that exists only because of it, as one transaction.

        Reviewer decisions, notes and sync history go with it, which is why ``confirm`` must repeat
        the mapping name. The audit trail stays: it is the record that the work existed, and the
        ``mapping.deleted`` entry says who removed it and how much went.
        """
        usage = self.mapping_usage(mapping_id)
        name = usage["mapping"]["name"]
        if (confirm or "").strip() != name:
            raise ValueError("to delete this mapping, confirm with its exact name")
        with self.db.transaction():
            for table, where in self._MAPPING_ROWS:
                self.db.execute(f"DELETE FROM {table} WHERE {where}", (mapping_id,))
            self.db.execute("DELETE FROM mappings WHERE id=?", (mapping_id,))
            self.db.audit(actor, "mapping.deleted", "mapping", mapping_id,
                          {"name": name, "counts": usage["counts"]}, now())
        return {"deleted": mapping_id, "name": name, "counts": usage["counts"],
                "reclaimed_bytes": self.db.compact()}

    # --- snapshots ----------------------------------------------------------
    def current_snapshot(self, mapping_id: int, side: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT * FROM snapshots WHERE mapping_id=? AND side=? AND status='ok' ORDER BY id DESC LIMIT 1",
            (mapping_id, side),
        )

    def snapshot_components(self, snapshot_id: int) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM components WHERE snapshot_id=? ORDER BY name, version", (snapshot_id,))

    def snapshot_component_keys(self, snapshot_id: int) -> set[str]:
        """Just the identity of a snapshot's components: presence checks need no rows or blobs."""
        return {r["external_id"] for r in
                self.db.query("SELECT external_id FROM components WHERE snapshot_id=?", (snapshot_id,))}

    def listing_component(self, mapping_id: int, key: str) -> dict[str, Any] | None:
        """A PL component the SL scope left out of the current snapshot: identity only."""
        snap = self.current_snapshot(mapping_id, "pl")
        if not snap:
            return None
        return self.db.one("SELECT external_id, name, version FROM pl_listing WHERE snapshot_id=? AND external_id=?",
                           (snap["id"], key))

    def listing_components(self, mapping_id: int) -> list[dict[str, Any]]:
        snap = self.current_snapshot(mapping_id, "pl")
        if not snap:
            return []
        return self.db.query("SELECT external_id, name, version FROM pl_listing WHERE snapshot_id=? ORDER BY name, version",
                             (snap["id"],))

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

    def _refresh_sides(self, mapping_id: int) -> dict[str, tuple[int, tuple[str, ...]]]:
        """SL first: what the scanner found decides which PL components are worth reading."""
        m = self.mapping(mapping_id)
        return {"sl": (m["sl_connection_id"], (m["sl_scan"],)),
                "pl": (m["pl_connection_id"], (m["pl_project"], m["pl_version"] or ""))}

    def _reviewed_pl_keys(self, mapping_id: int) -> set[str]:
        """PL components that carry reviewer work: a link someone made, or a decision or note on it.
        They stay in the PL snapshot whatever the SL side holds, so that work never loses its subject."""
        rows = self.db.query(
            "SELECT DISTINCT m.pl_key FROM matches m WHERE m.mapping_id=? AND m.pl_key != '' AND ("
            " m.status IN ('confirmed','manual')"
            " OR EXISTS (SELECT 1 FROM decisions d WHERE d.match_id=m.id)"
            " OR EXISTS (SELECT 1 FROM notes n WHERE n.match_id=m.id))", (mapping_id,))
        return {r["pl_key"] for r in rows}

    @staticmethod
    def _fetch_scoped(conn: Any, args: tuple[str, ...], scope: SLScope | None,
                      stats: dict[str, Any] | None) -> tuple[list[ComponentData], int]:
        """(inventory, components listed). A connector that can filter before its detail reads does;
        any other is filtered after, so every PL tool ends with the same scoped snapshot."""
        if scope is None:
            inventory = conn.fetch_inventory(*args)
            return inventory, len(inventory)
        if getattr(conn, "supports_scope", False):
            inventory = conn.fetch_inventory(*args, scope=scope)
            listed = (stats or {}).get("listed")
            return inventory, len(inventory) if listed is None else listed
        full = conn.fetch_inventory(*args)
        return [c for c in full if scope.admits_component(c)], len(full)

    def refresh(self, mapping_id: int, actor: str = "reviewer", *, full: bool = True,
                on_progress: Callable[[str, int, int | None], None] | None = None,
                cancel: threading.Event | None = None) -> dict[str, Any]:
        """Read both sides and re-match. ``full=False`` lets a connector reuse what it already read
        for unchanged items; ``on_progress(side, done, total)`` and ``cancel`` serve background jobs.

        All or nothing. Both sides are read first, over the network, with nothing stored, so a cancel
        or a crash there leaves the database as it was. Then one transaction stores both snapshots,
        re-matches and prunes: a reader sees the old pair or the new pair, never one new side
        matched against an old one. Once the commit starts (``on_progress("commit", ...)``) the
        refresh can no longer be cancelled.
        """
        sides = self._refresh_sides(mapping_id)
        fetched: dict[str, tuple[str, list[ComponentData] | None, str | None]] = {}
        listings: dict[str, list[tuple[str, str, str | None]]] = {}
        timing: dict[str, dict[str, Any]] = {}
        for side, (cid, args) in sides.items():
            if cancel is not None and cancel.is_set():
                raise RefreshCancelled()
            conn = self.connector_for(cid)
            scope = None
            if side == "pl":
                sl_inventory = fetched["sl"][1]
                if sl_inventory is None:
                    # Without the SL side there is no scope, and reading the whole PL inventory to
                    # match it against nothing would cost the most for the least: keep the old one.
                    fetched["pl"] = (conn.tool, None, None)
                    continue
                scope = SLScope(sl_inventory, keep=self._reviewed_pl_keys(mapping_id))
            if full and hasattr(conn, "clear_cache"):
                conn.clear_cache()
            if on_progress:
                on_progress(side, 0, None)
            report = (lambda d, t, side=side: on_progress(side, d, t)) if on_progress else None
            reporting = conn.reporting(report, cancel) if hasattr(conn, "reporting") else nullcontext(None)
            started = time.monotonic()
            try:
                with reporting as stats:
                    inventory, listed = self._fetch_scoped(conn, args, scope, stats if isinstance(stats, dict) else None)
                fetched[side] = (conn.tool, inventory, None)
                if scope is not None:
                    listings[side] = scope.skipped
                read = stats.get("read") if isinstance(stats, dict) else None
                timing[side] = {"seconds": round(time.monotonic() - started, 3), "count": len(inventory),
                                "read": len(inventory) if read is None else read, "full": full}
                if scope is not None:
                    timing[side].update({"listed": listed, "out_of_scope": max(0, listed - len(inventory))})
            except ConnectorError as exc:
                fetched[side] = (conn.tool, None, str(exc))
        if cancel is not None and cancel.is_set():
            raise RefreshCancelled()
        if on_progress:
            on_progress("commit", 0, None)
        results: dict[str, Any] = {}
        with self.db.transaction():
            for side, (tool, inventory, error) in fetched.items():
                if inventory is None and error is None:
                    results[side] = {"status": "skipped",
                                     "reason": "the SL side could not be read; the previous PL snapshot is kept"}
                elif error is None:
                    sid = self._store_snapshot(mapping_id, side, tool, inventory, listings.get(side, ()))
                    results[side] = {"status": "ok", "count": len(inventory), "snapshot_id": sid, "retrieved_at": now()}
                else:
                    sid = self.db.execute(
                        "INSERT INTO snapshots(mapping_id,side,tool,retrieved_at,status,error) VALUES(?,?,?,?,?,?)",
                        (mapping_id, side, tool, now(), "error", error),
                    ).lastrowid
                    results[side] = {"status": "error", "error": error, "snapshot_id": sid}
            matching = self._run_matching(mapping_id)
            matching["snapshots_pruned"] = self.prune_snapshots(mapping_id)
            self.db.audit(actor, "snapshot.refreshed", "mapping", mapping_id,
                          {"sides": {k: v.get("status") for k, v in results.items()}, "matching": matching,
                           "timing": timing}, now())
        return {"mapping_id": mapping_id, "sides": results, "matching": matching, "timing": timing}

    def _refresh_history(self, mapping_id: int) -> dict[str, dict[str, Any]]:
        """Each side's most recent successful read: the calibration for an estimate."""
        out: dict[str, dict[str, Any]] = {}
        rows = self.db.query(
            "SELECT at, detail_json FROM audit WHERE action='snapshot.refreshed' AND entity_type='mapping'"
            " AND entity_id=? ORDER BY id DESC LIMIT ?", (str(mapping_id), REFRESH_HISTORY))
        for row in rows:
            timing = json.loads(row["detail_json"]).get("timing") or {}
            for side, t in timing.items():
                if side not in out and isinstance(t, dict) and "seconds" in t:
                    out[side] = {**t, "at": row["at"]}
        return out

    def _scope_plan(self, mapping_id: int, entry: dict[str, Any], sl: dict[str, Any]) -> None:
        """PL details are read only for what the SL side could match; count that before the read.

        The SL scan's own count is the honest proxy: the matcher pairs each SL component with at most
        one PL component, so roughly one PL read per SL item (plus any already-reviewed component).
        """
        snap = self.current_snapshot(mapping_id, "sl")
        sl_items = sl.get("items") if sl.get("items") is not None else (snap["item_count"] if snap else None)
        if sl_items is None:
            entry["work"] = f"{entry['work']}, only for components matching the SL side (counted once SL is read)"
            entry["to_read"] = None
            return
        scoped = sl_items + len(self._reviewed_pl_keys(mapping_id))
        if entry.get("items") is not None:
            scoped = min(entry["items"], scoped)
        entry["scoped"] = True
        entry["to_read"] = scoped
        rest = f"; the other {entry['items'] - scoped} are listed but not read" if entry.get("items") else \
            "; the rest are listed but not read"
        entry["work"] = f"the listing, then details only for about {scoped} that can match the SL side{rest}"

    def refresh_plan(self, mapping_id: int, full: bool = False) -> dict[str, Any]:
        """What a refresh will read and how long it should take, before anything starts.

        Each connector that can (``refresh_plan``) counts its work with one cheap request. The time
        is calibrated on this mapping's last refresh: seconds per item actually read, times the
        items to read now. Without a previous read there is no honest estimate, and the plan says
        so instead of guessing.
        """
        m = self.mapping(mapping_id)
        history = self._refresh_history(mapping_id)
        targets = {"pl": f"{m['pl_project']} {m['pl_version'] or ''}".strip(), "sl": m["sl_scan"]}
        plan_sides: dict[str, dict[str, Any]] = {}
        total, known = 0.0, True
        for side, (cid, args) in self._refresh_sides(mapping_id).items():
            conn = self.connector_for(cid)
            entry: dict[str, Any] = {"tool": conn.tool, "target": targets[side], "items": None,
                                     "unit": "components", "to_read": None, "cached": 0,
                                     "work": "the full inventory", "error": None, "last": history.get(side)}
            plan_fn = getattr(conn, "refresh_plan", None)
            if plan_fn:
                try:
                    entry.update(plan_fn(*args, full=full))
                except ConnectorError as exc:
                    entry["error"] = str(exc)
            if side == "pl":
                self._scope_plan(mapping_id, entry, plan_sides.get("sl", {}))
            last = entry["last"]
            estimate: float | None = None
            if entry["to_read"] == 0:
                estimate = 0.0
            elif last and entry["to_read"] is not None and last.get("read"):
                estimate = entry["to_read"] * last["seconds"] / last["read"]
            elif last and entry["to_read"] is None and not entry["error"]:
                estimate = float(last["seconds"])   # the tool cannot count; assume last time's work
            entry["estimate_seconds"] = None if estimate is None else round(estimate, 1)
            if estimate is None:
                known = False
            else:
                total += estimate
            plan_sides[side] = entry
        estimate_total = round(total, 1) if known else None
        return {"mapping_id": mapping_id, "full": full, "sides": plan_sides,
                "estimate_seconds": estimate_total, "threshold_seconds": LONG_REFRESH_SECONDS,
                "needs_approval": estimate_total is None or estimate_total >= LONG_REFRESH_SECONDS}

    def _store_snapshot(self, mapping_id: int, side: str, tool: str, inventory: list[ComponentData],
                        listing: Any = ()) -> int:
        """Store one side's inventory as one unit: a half-stored snapshot would be matched.

        ``listing`` is the (key, name, version) of PL components the SL scope left out; they are
        stored beside the snapshot for the manual-match picker only.
        """
        version_hash = sha256(json.dumps(sorted(source_version(c) for c in inventory), sort_keys=True))
        with self.db.transaction():
            sid = self._store_snapshot_rows(mapping_id, side, tool, inventory, version_hash)
            for key, name, version in listing:
                self.db.execute("INSERT INTO pl_listing(snapshot_id,external_id,name,version) VALUES(?,?,?,?)",
                                (sid, key, name, version))
            return sid

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

    def snapshot_versions(self, mapping_id: int, side: str, previous: bool = False) -> dict[str, str]:
        """{external_id: source_version} for this side's latest (or previous) successful snapshot."""
        snap = self.db.one(
            "SELECT id FROM snapshots WHERE mapping_id=? AND side=? AND status='ok' ORDER BY id DESC LIMIT 1 OFFSET ?",
            (mapping_id, side, 1 if previous else 0))
        if not snap:
            return {}
        rows = self.db.query("SELECT external_id, source_version FROM components WHERE snapshot_id=?", (snap["id"],))
        return {r["external_id"]: r["source_version"] for r in rows}

    def prune_snapshots(self, mapping_id: int, keep: int = SNAPSHOTS_PER_SIDE) -> int:
        """Keep the newest `keep` snapshots per side; the FK cascade takes their components and rows.

        Without this every refresh copies the whole inventory again and the tables grow forever,
        while every read only ever looks at the newest one.
        """
        pruned = 0
        with self.db.transaction():
            for side in ("pl", "sl"):
                cur = self.db.execute(
                    "DELETE FROM snapshots WHERE mapping_id=? AND side=? AND id NOT IN"
                    " (SELECT id FROM snapshots WHERE mapping_id=? AND side=? ORDER BY id DESC LIMIT ?)",
                    (mapping_id, side, mapping_id, side, keep))
                pruned += cur.rowcount
        return pruned

    def prune_stale_matches(self, mapping_id: int, pl_snapshot_id: int, sl_snapshot_id: int) -> int:
        """Delete match rows whose components exist on neither side and that nobody touched.

        A match that is merely stale (one side still present) is a finding and stays. A row with
        nothing on either side is garbage from a component that no longer exists. Reviewer work
        outlives the snapshot it was made against, so any row with a decision, note or suggestion,
        a confirmed status or a review state is kept.
        """
        pl_keys = self.snapshot_component_keys(pl_snapshot_id)
        sl_keys = self.snapshot_component_keys(sl_snapshot_id)
        candidates = [r["id"] for r in self.db.query(
            "SELECT id, pl_key, sl_key FROM matches WHERE mapping_id=?"
            " AND status != 'confirmed' AND (review_state IS NULL OR review_state = '')", (mapping_id,))
            if not ((r["pl_key"] and r["pl_key"] in pl_keys) or (r["sl_key"] and r["sl_key"] in sl_keys))]
        if not candidates:
            return 0
        touched: set[int] = set()
        for table in ("decisions", "notes", "suggestions"):
            for chunk in _chunks(candidates):
                marks = ",".join("?" * len(chunk))
                rows = self.db.query(f"SELECT DISTINCT match_id FROM {table} WHERE match_id IN ({marks})", tuple(chunk))
                touched |= {r["match_id"] for r in rows}
        dead = [i for i in candidates if i not in touched]
        for chunk in _chunks(dead):
            marks = ",".join("?" * len(chunk))
            self.db.execute(f"DELETE FROM matches WHERE id IN ({marks})", tuple(chunk))
        return len(dead)

    def mapping_inventory(self, mapping_id: int) -> dict[str, Any]:
        """Both sides with observations plus the loaded SL license files, in a fixed few queries.

        What ``ReviewService.comparison`` takes as ``inventory``: any whole-mapping pass hands this in
        instead of letting each component read its own rows (one query per table per component).
        """
        sides = {side: self.side_components_with_observations(mapping_id, side) for side in ("pl", "sl")}
        loaded: dict[str, list[dict[str, Any]]] = {}
        for f in self.db.query("SELECT * FROM sl_license_files WHERE mapping_id=? ORDER BY file_path", (mapping_id,)):
            loaded.setdefault(f["sl_key"], []).append(f)
        return {**sides, "loaded": loaded}

    def _run_matching(self, mapping_id: int) -> dict[str, Any]:
        pl_snap = self.current_snapshot(mapping_id, "pl")
        sl_snap = self.current_snapshot(mapping_id, "sl")
        inventory = self.mapping_inventory(mapping_id)
        sides, loaded = {s: inventory[s] for s in ("pl", "sl")}, inventory["loaded"]
        pl = [self._to_component_data(e) for e in sides["pl"].values()]
        sl = [self._to_component_data(e) for e in sides["sl"].values()]
        if not pl_snap or not sl_snap:
            return {"status": "partial", "pl": len(pl), "sl": len(sl), "reason": "one side has no successful snapshot"}
        pl_by_key = {c.external_id: c for c in pl}
        sl_by_key = {c.external_id: c for c in sl}
        prior = self.db.query("SELECT * FROM matches WHERE mapping_id=?", (mapping_id,))
        # A reviewer's confirmed pair is not re-opened for matching, and a rejected pair is not proposed again.
        pinned = [r for r in prior if r["status"] == "confirmed"
                  and r["pl_key"] in pl_by_key and r["sl_key"] in sl_by_key]
        blocked = {(r["pl_key"], r["sl_key"]) for r in prior if r["status"] == "rejected"}
        pinned_pl = {r["pl_key"] for r in pinned}
        pinned_sl = {r["sl_key"] for r in pinned}
        results = match_components([c for c in pl if c.external_id not in pinned_pl],
                                   [c for c in sl if c.external_id not in pinned_sl], blocked)
        for r in pinned:
            plc, slc = pl_by_key[r["pl_key"]], sl_by_key[r["sl_key"]]
            results.append(MatchResult(pl_key=plc.external_id, sl_key=slc.external_id, method=r["method"],
                                       confidence=r["confidence"], status="confirmed", pl_name=plc.name,
                                       pl_version=plc.version, sl_name=slc.name, sl_version=slc.version))
        from .review import RecomputeContext, ReviewService, decisions_by_match  # deferred: module cycle
        license_map = self.license_map_lookup()
        rules = self.db.query(
            "SELECT * FROM false_positive_rules WHERE revoked_at IS NULL "
            "AND (scope='global' OR mapping_id=?) ORDER BY id",
            (mapping_id,),
        )
        with self.db.transaction():
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
                        "INSERT INTO matches(mapping_id,pl_key,sl_key,method,confidence,status,rationale_json,"
                        "mismatch_json,pl_name,pl_version,sl_name,sl_version,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (mapping_id, r.pl_key, r.sl_key, r.method, r.confidence, r.status,
                         json.dumps(r.rationale), json.dumps(r.mismatch_reasons), r.pl_name, r.pl_version,
                         r.sl_name, r.sl_version, now(), now()),
                    )
                    added += 1
                    changed.append(cur.lastrowid)
            if changed:
                review = ReviewService(self)
                rows = {r["id"]: r for r in self.db.query("SELECT * FROM matches WHERE mapping_id=?", (mapping_id,))}
                decisions = decisions_by_match(self.db, changed)
                for match_id in changed:
                    review.recompute(match_id, context=RecomputeContext(
                        match=rows[match_id], decisions=decisions.get(match_id, []), inventory={**sides, "loaded": loaded},
                        license_map=license_map, rules=rules))
            # An unmatched row whose component has since been paired elsewhere is superseded.
            proposed = {r.pl_key + "\x00" + r.sl_key for r in results}
            for pair, row in existing.items():
                if row["status"] == "unmatched" and pair not in proposed:
                    self.db.execute(
                        "DELETE FROM matches WHERE id=? AND NOT EXISTS (SELECT 1 FROM decisions WHERE match_id=matches.id)"
                        " AND NOT EXISTS (SELECT 1 FROM notes WHERE match_id=matches.id)"
                        " AND NOT EXISTS (SELECT 1 FROM suggestions WHERE match_id=matches.id)", (row["id"],))
            pruned = self.prune_stale_matches(mapping_id, pl_snap["id"], sl_snap["id"])
        return {"status": "ok", "pl": len(pl), "sl": len(sl), "proposed": len(results),
                "new_matches": added, "matches_pruned": pruned}

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
        timing = self._refresh_history(mapping_id).get("pl") or {}
        sides["pl"]["listed"] = timing.get("listed")
        sides["pl"]["out_of_scope"] = timing.get("out_of_scope")
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
        pl_keys = self.snapshot_component_keys(pl_snap["id"]) if pl_snap else set()
        sl_keys = self.snapshot_component_keys(sl_snap["id"]) if sl_snap else set()
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
