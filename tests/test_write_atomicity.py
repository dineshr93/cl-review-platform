"""A10: multi-statement writes commit as a unit or not at all."""
from __future__ import annotations

import unittest
from unittest import mock

from clreview.db import DB
from clreview.service import now
from clreview.sync import preview_match

from helpers import make_review, make_service, tmpdir

from test_review import base_components
from test_sync import approve_all, components


class Boom(Exception):
    """The simulated mid-write failure."""


def fail_on(db, prefix, nth=1):
    """Raise on the `nth` statement whose SQL starts with `prefix`."""
    real = db.execute
    seen = {"n": 0}

    def wrapper(sql, params=()):
        if sql.strip().upper().startswith(prefix.upper()):
            seen["n"] += 1
            if seen["n"] >= nth:
                raise Boom(f"simulated failure at {prefix!r} #{seen['n']}")
        return real(sql, params)

    return mock.patch.object(db, "execute", wrapper)


def count(db, table):
    return db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]


class TransactionHelperTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def tearDown(self):
        self.db.close()

    def add(self, action):
        self.db.audit("t", action, "test", 1, {}, now())

    def test_plain_execute_still_commits(self):
        self.add("plain")
        self.assertEqual(count(self.db, "audit"), 1)

    def test_committed_group_is_kept(self):
        with self.db.transaction():
            self.add("a")
            self.add("b")
        self.assertEqual(count(self.db, "audit"), 2)

    def test_rolled_back_group_leaves_nothing(self):
        with self.assertRaises(Boom):
            with self.db.transaction():
                self.add("a")
                raise Boom("mid-write")
        self.assertEqual(count(self.db, "audit"), 0)

    def test_rows_are_visible_inside_the_block(self):
        with self.db.transaction():
            self.add("a")
            self.assertEqual(count(self.db, "audit"), 1)

    def test_nested_block_does_not_commit_early(self):
        with self.assertRaises(Boom):
            with self.db.transaction():
                self.add("outer")
                with self.db.transaction():
                    self.add("inner")
                self.assertTrue(self.db.in_transaction())
                raise Boom("after the inner block")
        self.assertEqual(count(self.db, "audit"), 0)
        self.assertFalse(self.db.in_transaction())


class SnapshotAtomicityTests(unittest.TestCase):
    TABLES = ("snapshots", "components", "copyrights", "licenses", "license_texts")

    def setUp(self):
        self.pl, self.sl = base_components()
        self.svc, self.db, self.mid = make_service(tmpdir(), self.pl, self.sl)
        m = self.svc.mapping(self.mid)
        conn = self.svc.connector_for(m["sl_connection_id"])
        self.inventory = conn.fetch_inventory(m["sl_scan"])

    def tearDown(self):
        self.db.close()

    def counts(self):
        return {t: count(self.db, t) for t in self.TABLES}

    def test_a_failed_snapshot_leaves_no_partial_rows(self):
        before = self.counts()
        self.assertTrue(any(c.license_texts for c in self.inventory))
        with fail_on(self.db, "INSERT INTO license_texts"):
            with self.assertRaises(Boom):
                self.svc._store_snapshot(self.mid, "sl", "mock-sl", self.inventory)
        self.assertEqual(self.counts(), before)

    def test_a_successful_snapshot_is_stored_whole(self):
        before = self.counts()
        sid = self.svc._store_snapshot(self.mid, "sl", "mock-sl", self.inventory)
        self.assertTrue(sid)
        after = self.counts()
        self.assertEqual(after["snapshots"], before["snapshots"] + 1)
        self.assertGreater(after["license_texts"], before["license_texts"])


class SuggestionAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.pl, self.sl = base_components()
        self.svc, self.db, self.mid = make_service(tmpdir(), self.pl, self.sl)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        row = [r for r in self.review.detail(self.match_id)["comparison"]["copyrights"]
               if r["kind"] == "variant"][0]
        self.suggestion = self.review.add_suggestion(
            self.match_id, "copyright", row["key"], row["sl_text"], model="test")["id"]

    def tearDown(self):
        self.db.close()

    def test_a_failed_acceptance_leaves_no_decision(self):
        audit0, decisions0 = count(self.db, "audit"), count(self.db, "decisions")
        with fail_on(self.db, "UPDATE suggestions"):
            with self.assertRaises(Boom):
                self.review.decide_suggestion(self.suggestion, "accept", reviewer="alice")
        self.assertEqual(count(self.db, "decisions"), decisions0)
        self.assertEqual(count(self.db, "audit"), audit0)
        self.assertEqual(self.review.suggestions(self.match_id)[0]["status"], "open")

    def test_a_successful_acceptance_persists_everything(self):
        out = self.review.decide_suggestion(self.suggestion, "accept", reviewer="alice")
        self.assertEqual(out["status"], "accepted")
        self.assertEqual(count(self.db, "decisions"), 1)
        self.assertEqual(self.review.suggestions(self.match_id)[0]["status"], "accepted")


class JobAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.pl, self.sl = components()
        self.svc, self.db, self.mid = make_service(tmpdir(), self.pl, self.sl)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        approve_all(self.review, self.match_id)

    def tearDown(self):
        self.db.close()

    def test_a_dry_run_writes_its_items(self):
        job = self.sync.create_job(self.mid, dry_run=True, actor="alice")
        self.assertEqual(job["status"], "dry_run")
        self.assertGreaterEqual(len(job["items"]), 2)

    def test_a_failed_job_leaves_no_orphan_rows(self):
        audit0 = count(self.db, "audit")
        match = self.svc._current_matches(self.mid)[0]
        decisions = self.db.query("SELECT * FROM decisions WHERE match_id=?", (match["id"],))
        self.assertGreaterEqual(len(preview_match(self.svc, match, decisions)), 2)
        with fail_on(self.db, "INSERT INTO sync_items", nth=2):
            with self.assertRaises(Boom):
                self.sync.create_job(self.mid, dry_run=True, actor="alice")
        self.assertEqual(count(self.db, "sync_jobs"), 0)
        self.assertEqual(count(self.db, "sync_items"), 0)
        self.assertEqual(count(self.db, "audit"), audit0)

    def test_a_successful_job_persists_everything(self):
        job = self.sync.create_job(self.mid, dry_run=True, actor="alice")
        self.assertEqual(count(self.db, "sync_jobs"), 1)
        self.assertEqual(count(self.db, "sync_items"), len(job["items"]))


if __name__ == "__main__":
    unittest.main()
