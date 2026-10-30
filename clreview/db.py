"""SQLite persistence: raw snapshots, review decisions, sync jobs, audit trail."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS connections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL, tool TEXT NOT NULL, name TEXT NOT NULL,
  base_url TEXT, config_json TEXT NOT NULL DEFAULT '{}',
  auth_env_json TEXT NOT NULL DEFAULT '{}',
  last_test_ok INTEGER NOT NULL DEFAULT 0, last_test_at TEXT, last_test_error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mappings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  pl_connection_id INTEGER NOT NULL REFERENCES connections(id),
  sl_connection_id INTEGER NOT NULL REFERENCES connections(id),
  pl_project TEXT NOT NULL, pl_version TEXT, sl_scan TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL REFERENCES mappings(id) ON DELETE CASCADE,
  side TEXT NOT NULL, tool TEXT NOT NULL, retrieved_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok', error TEXT,
  item_count INTEGER NOT NULL DEFAULT 0, version_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots(mapping_id, side, status);
CREATE TABLE IF NOT EXISTS components(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
  side TEXT NOT NULL, tool TEXT NOT NULL, external_id TEXT NOT NULL,
  name TEXT NOT NULL, version TEXT, purl TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}', raw_json TEXT NOT NULL DEFAULT '{}',
  source_version TEXT NOT NULL DEFAULT '', write_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS components_key ON components(snapshot_id, external_id);
CREATE TABLE IF NOT EXISTS copyrights(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
  text TEXT NOT NULL, normalized TEXT NOT NULL, hash TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT '', source_path TEXT,
  active INTEGER NOT NULL DEFAULT 1, retrieved_at TEXT
);
CREATE TABLE IF NOT EXISTS licenses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
  name TEXT NOT NULL, spdx TEXT, license_type TEXT NOT NULL DEFAULT 'declared',
  evidence_paths_json TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT '', retrieved_at TEXT
);
CREATE TABLE IF NOT EXISTS license_texts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, file_name TEXT, file_path TEXT,
  text TEXT NOT NULL, content_hash TEXT NOT NULL,
  provenance TEXT NOT NULL DEFAULT '', is_license_file INTEGER NOT NULL DEFAULT 0,
  retrieved_at TEXT
);
CREATE TABLE IF NOT EXISTS matches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL REFERENCES mappings(id) ON DELETE CASCADE,
  pl_key TEXT NOT NULL DEFAULT '', sl_key TEXT NOT NULL DEFAULT '',
  method TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'auto',
  rationale_json TEXT NOT NULL DEFAULT '[]', mismatch_json TEXT NOT NULL DEFAULT '[]',
  pl_name TEXT NOT NULL DEFAULT '', pl_version TEXT, sl_name TEXT NOT NULL DEFAULT '', sl_version TEXT,
  review_state TEXT, discrepancy_count INTEGER NOT NULL DEFAULT 0,
  review_status TEXT NOT NULL DEFAULT 'not_reviewed',
  confirmed_by TEXT, confirmed_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS matches_pair ON matches(mapping_id, pl_key, sl_key);
CREATE INDEX IF NOT EXISTS matches_mapping ON matches(mapping_id);  -- narrower than matches_pair: the planner picks it for `SELECT * FROM matches WHERE mapping_id=?` and it keeps `... ORDER BY id` sort-free (verified with EXPLAIN QUERY PLAN; dropping it costs a temp B-tree)
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL, match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  field TEXT NOT NULL, entry_key TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL, approved_value TEXT, reviewer TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', source_version TEXT NOT NULL DEFAULT '',
  pl_source_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS decisions_key ON decisions(match_id, field, entry_key);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL, match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  reviewer TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_match ON notes(match_id);
CREATE TABLE IF NOT EXISTS sync_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL REFERENCES mappings(id) ON DELETE CASCADE,
  dry_run INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending',
  created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  finished_at TEXT, summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sync_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
  match_id INTEGER NOT NULL, field TEXT NOT NULL, entry_key TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending', pl_before TEXT, approved_value TEXT,
  api_status INTEGER, api_response TEXT, message TEXT NOT NULL DEFAULT '',
  verified_value TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sync_items_job ON sync_items(job_id);
CREATE TABLE IF NOT EXISTS suggestions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL, match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  field TEXT NOT NULL, entry_key TEXT NOT NULL DEFAULT '',
  value TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL,
  decided_by TEXT, decided_at TEXT
);
CREATE INDEX IF NOT EXISTS suggestions_match ON suggestions(match_id);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT '', entity_id TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_entity ON audit(entity_type, entity_id);
"""

#: The shape of ``SCHEMA``, stamped into ``PRAGMA user_version`` on every database this code
#: opens. ``SCHEMA`` alone cannot protect an existing database: every statement in it is
#: ``CREATE ... IF NOT EXISTS``, so a column added to the text later is silently *not* added
#: to a database that already exists, and the first query that names it fails at runtime —
#: several requests after the deploy, not during it.
#: Bump this with any schema change and write the migration that brings an older database to
#: the new shape. There is none to run at version 1; when one is needed, run it between
#: ``_refuse_newer_schema`` and ``executescript``, keyed on the stored version. Databases
#: created before versioning existed report 0 and are already at this shape, so 0 is adopted
#: rather than rejected.
SCHEMA_VERSION = 1


class DB:
    """One shared connection, serialized.

    The HTTP server is threaded and sqlite3 cursors are not safe to share, so
    every statement takes this lock; without it concurrent requests raise
    ``InterfaceError: bad parameter or other API misuse``.
    """

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._tx_depth = 0
        self.commits = 0  # how many times data actually reached the file; tests assert on it
        # The schema's ON DELETE CASCADE clauses are inert unless every connection asks for them.
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._refuse_newer_schema()
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._stamp_schema_version()

    def _stored_schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _refuse_newer_schema(self) -> None:
        """Refuse a database shaped by a newer release, before touching it.

        Checked ahead of ``executescript`` on purpose: the script is idempotent, so running
        it first would already have written this build's tables and indexes into a database it
        only half understands.
        """
        stored = self._stored_schema_version()
        if stored > SCHEMA_VERSION:
            self.conn.close()
            raise RuntimeError(
                f"database {self.path} is schema version {stored}, this build understands "
                f"{SCHEMA_VERSION}; upgrade cl-review-platform before opening it")

    def _stamp_schema_version(self) -> None:
        """Record the shape just created, adopting an unversioned database silently.

        A database at version 0 was written before versioning existed and already has this
        shape, so adopting it is correct rather than a silent migration.
        """
        if self._stored_schema_version() != SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            if self._tx_depth == 0:  # inside transaction() the commit waits for the block
                self.conn.commit()
                self.commits += 1
        return cur

    def in_transaction(self) -> bool:
        return self._tx_depth > 0

    @contextmanager
    def transaction(self) -> Iterator["DB"]:
        """Commit a group of statements together, or not at all.

        ``execute`` commits per statement by default, so a write that dies halfway leaves a
        half-applied row set behind. This holds the write lock for the group, commits once at
        the end and rolls the whole group back on any error. Keep network calls out of the
        block: the lock is held for everything inside it.
        """
        with self.lock:
            outermost = self._tx_depth == 0
            self._tx_depth += 1
            try:
                if outermost:
                    self.conn.execute("BEGIN")
                yield self
                if outermost:
                    self.conn.commit()
                    self.commits += 1
            except BaseException:
                if outermost:
                    self.conn.rollback()
                raise
            finally:
                self._tx_depth -= 1

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def audit(self, actor: str, action: str, entity_type: str, entity_id: str | int,
              detail: dict[str, Any], at: str) -> None:
        self.execute(
            "INSERT INTO audit(at,actor,action,entity_type,entity_id,detail_json) VALUES(?,?,?,?,?,?)",
            (at, actor, action, entity_type, str(entity_id), json.dumps(detail)),
        )
