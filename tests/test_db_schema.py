"""Schema guarantees: the indexes the request paths need and the FK cascade the schema promises."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from clreview.db import DB
from clreview.server import seed_demo

NEW_INDEXES = ("snapshots_lookup", "matches_mapping", "notes_match", "sync_items_job",
               "suggestions_match", "audit_entity")
CASCADED = ("snapshots", "components", "copyrights", "licenses", "license_texts", "matches",
            "decisions", "notes", "suggestions", "sync_jobs", "sync_items")


def index_names(db: DB) -> set[str]:
    rows = db.query("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
    return {r["name"] for r in rows}


class SchemaIndexTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def tearDown(self):
        self.db.close()

    def test_every_hot_path_index_exists(self):
        missing = set(NEW_INDEXES) - index_names(self.db)
        self.assertFalse(missing, f"missing indexes: {sorted(missing)}")

    def test_planner_uses_the_new_indexes(self):
        for label, sql in (
            ("snapshots_lookup", "SELECT * FROM snapshots WHERE mapping_id=1 AND side='pl' AND status='ok'"),
            ("sync_items_job", "SELECT * FROM sync_items WHERE job_id=1"),
            ("notes_match", "SELECT * FROM notes WHERE match_id=1"),
            ("suggestions_match", "SELECT * FROM suggestions WHERE match_id=1"),
            ("audit_entity", "SELECT * FROM audit WHERE entity_type='match' AND entity_id='1'"),
        ):
            plan = " ".join(r["detail"] for r in self.db.query("EXPLAIN QUERY PLAN " + sql))
            self.assertIn(label, plan, f"{label} not used: {plan}")

    def test_database_created_before_the_change_upgrades_on_next_open(self):
        path = os.path.join(tempfile.mkdtemp(prefix="clreview-test-"), "legacy.db")
        self.addCleanup(shutil.rmtree, path.rsplit(os.sep, 1)[0], ignore_errors=True)
        DB(path).close()
        con = sqlite3.connect(path)
        con.executescript("DROP INDEX IF EXISTS notes_match; DROP INDEX IF EXISTS audit_entity;")
        con.close()
        self.db.close()
        self.db = DB(path)  # a database file written by an older release
        self.assertFalse(set(NEW_INDEXES) - index_names(self.db))


class ForeignKeyTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.mid = seed_demo(self.db)["mapping_id"]
        match = self.db.one("SELECT id FROM matches LIMIT 1")["id"]
        self.db.execute("INSERT INTO decisions(mapping_id,match_id,field,action,created_at) VALUES(?,?,'copyright','approve_sl','now')",
                        (self.mid, match))
        self.db.execute("INSERT INTO notes(mapping_id,match_id,body,created_at) VALUES(?,?,'note','now')", (self.mid, match))
        self.db.execute("INSERT INTO suggestions(mapping_id,match_id,field,value,created_at) VALUES(?,?,'copyright','x','now')",
                        (self.mid, match))
        self.db.execute("INSERT INTO sync_jobs(mapping_id,dry_run,status,created_at) VALUES(?,0,'completed','now')", (self.mid,))
        job = self.db.one("SELECT id FROM sync_jobs ORDER BY id DESC LIMIT 1")["id"]
        self.db.execute("INSERT INTO sync_items(job_id,match_id,field,status,updated_at) VALUES(?,?,'copyright','succeeded','now')",
                        (job, match))

    def tearDown(self):
        self.db.close()

    def test_foreign_keys_are_on_for_every_connection(self):
        self.assertEqual(self.db.query("PRAGMA foreign_keys")[0]["foreign_keys"], 1)

    def test_fresh_database_has_no_foreign_key_violations(self):
        self.assertEqual(self.db.query("PRAGMA foreign_key_check"), [])

    def test_dangling_child_row_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO snapshots(mapping_id,side,tool,retrieved_at) VALUES(999999,'pl','x','now')")

    def test_deleting_a_mapping_leaves_no_orphan_rows(self):
        self.assertTrue(self.db.query("SELECT id FROM snapshots"))
        self.db.execute("DELETE FROM mappings WHERE id=?", (self.mid,))
        for table in CASCADED:
            self.assertEqual(self.db.query(f"SELECT id FROM {table}"), [], f"{table} kept orphan rows")
        # the audit trail is deliberately not a child of a mapping: it outlives it
        self.assertTrue(self.db.query("SELECT id FROM audit"))


if __name__ == "__main__":
    unittest.main()
