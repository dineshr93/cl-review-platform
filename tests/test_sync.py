"""Synchronization tests: dry-run, writes, verification, idempotency, limits, retries."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from clreview.domain import sha256

from helpers import (GENERIC_ZLIB, SOURCE_ZLIB, make_review, make_service, paths_for, pl_component,
                     register_readonly_pl, rewrite_fixtures, tmpdir)

from test_review import pair_components


def components(fail_writes=False):
    pl = [pl_component(
        name="zlib", version="1.2.11",
        licenses=[{"name": "Zlib", "spdx": "Zlib"}],
        copyrights=[{"text": "Copyright (C) 1995-2017 Jean-loup Gailly and Mark Adler", "origin": "origin"}],
        license_texts=[{"kind": "generic_metadata", "text": GENERIC_ZLIB, "provenance": "kb"}],
        fail_writes_once=fail_writes,
    )]
    sl = [{
        "external_id": "zlib-1.2.11", "name": "zlib", "version": "1.2.11",
        "licenses": [{"name": "Zlib", "spdx": "Zlib"}, {"name": "MIT", "spdx": "MIT"}],
        "copyrights": [{"text": "Copyright  1995-2017  Jean-loup Gailly and Mark Adler"}],
        "license_texts": [{"kind": "source_file", "file_name": "LICENSE", "file_path": "zlib/LICENSE",
                           "is_license_file": True, "provenance": "fossology item content", "text": SOURCE_ZLIB}],
    }]
    return pl, sl


def approve_all(review, match_id):
    d = review.detail(match_id)
    for r in d["comparison"]["copyrights"]:
        if r["kind"] in ("variant", "sl_only"):
            review.decide(match_id, "copyright", r["key"], "approve_sl", reviewer="alice")
        elif r["kind"] == "pl_only":
            review.decide(match_id, "copyright", r["key"], "accept_pl", reviewer="alice")
    for r in d["comparison"]["licenses"]:
        if r["kind"] in ("sl_only", "spdx_diff"):
            review.decide(match_id, "license_name", r["key"], "approve_sl", reviewer="alice")
        elif r["kind"] == "pl_only":
            review.decide(match_id, "license_name", r["key"], "accept_pl", reviewer="alice")
    tc = d["comparison"]["license_text"]
    if tc and tc["recommendation"] == "replace_with_source":
        review.decide(match_id, "license_text", tc["preferred"]["content_hash"], "approve_sl", reviewer="alice")
    review.set_review_state(match_id, "approved_for_sync", "alice")
    return d


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.pl, self.sl = components()
        self.svc, self.db, self.mid = make_service(self.dir, self.pl, self.sl)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        approve_all(self.review, self.match_id)

    def _pl_state(self):
        return self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"]).read_component("proj|1.0|zlib")

    def test_dry_run_writes_nothing(self):
        before = self._pl_state()
        job = self.sync.create_job(self.mid, dry_run=True)
        self.assertEqual(job["status"], "dry_run")
        self.assertEqual(self._pl_state(), before)
        self.assertTrue(all(i["status"] in ("dry_run", "ready", "already_applied") for i in job["items"]))

    def test_live_sync_writes_and_verifies(self):
        job = self.sync.create_job(self.mid, dry_run=False, confirm=True, actor="alice")
        self.assertEqual(job["status"], "completed")
        statuses = {i["status"] for i in job["items"]}
        self.assertEqual(statuses, {"succeeded"})
        self.assertTrue(all(i["verified_value"] for i in job["items"]))
        after = self._pl_state()
        self.assertIn("Copyright  1995-2017  Jean-loup Gailly and Mark Adler", after["copyrights"])
        self.assertIn("MIT", after["licenses"])
        self.assertEqual(after["license_texts"][0]["content_hash"], sha256(SOURCE_ZLIB))
        audit = self.db.query("SELECT action FROM audit")
        self.assertTrue(any(a["action"] == "sync.item" for a in audit))

    def test_repeated_sync_is_idempotent(self):
        self.sync.create_job(self.mid, dry_run=False, confirm=True)
        again = self.sync.create_job(self.mid, dry_run=False, confirm=True)
        self.assertEqual({i["status"] for i in again["items"]}, {"skipped_already_applied"})
        after = self._pl_state()
        self.assertEqual(after["copyrights"].count("Copyright  1995-2017  Jean-loup Gailly and Mark Adler"), 1)
        self.assertEqual(after["licenses"].count("MIT"), 1)

    def test_unsupported_target_is_reported_not_faked(self):
        register_readonly_pl()
        d = tmpdir()
        svc, db, mid = make_service(d, *components(), pl_tool="readonly-pl")
        review, sync = make_review(svc)
        match_id = review.list_components(mid)["items"][0]["id"]
        approve_all(review, match_id)
        job = sync.create_job(mid, dry_run=False, confirm=True, actor="alice")
        self.assertEqual({i["status"] for i in job["items"]}, {"unsupported_target"})
        self.assertTrue(all("unsupported" in i["message"] for i in job["items"]))
        self.assertEqual(job["summary"], {"unsupported_target": len(job["items"])})

    def test_api_failure_then_retry(self):
        d = tmpdir()
        svc, db, mid = make_service(d, *components(fail_writes=True))
        review, sync = make_review(svc)
        match_id = review.list_components(mid)["items"][0]["id"]
        approve_all(review, match_id)
        job = sync.create_job(mid, dry_run=False, confirm=True)
        self.assertTrue(any(i["status"] == "failed" for i in job["items"]))
        retry = sync.retry(job["id"], confirm=True, actor="alice")
        self.assertEqual({i["status"] for i in retry["items"]}, {"succeeded"})
        self.assertGreaterEqual(retry["items"][0]["attempts"], 1)

    def test_pl_change_after_review_is_a_conflict(self):
        paths = paths_for(self.svc, self.mid)
        changed = [pl_component(
            name="zlib", version="1.2.11",
            licenses=[{"name": "Zlib", "spdx": "Zlib"}],
            copyrights=[{"text": "Copyright 2024 Someone Else", "origin": "origin"}],
            license_texts=[{"kind": "generic_metadata", "text": GENERIC_ZLIB, "provenance": "kb"}],
        )]
        rewrite_fixtures(paths, pl_components=changed)
        self.svc.refresh(self.mid, "alice")
        job = self.sync.create_job(self.mid, dry_run=False, confirm=True)
        self.assertIn("conflict", {i["status"] for i in job["items"]})

    def test_sl_change_after_review_is_stale(self):
        paths = paths_for(self.svc, self.mid)
        sl = components()[1]
        sl[0]["copyrights"] = [{"text": "Copyright 2024 Someone Else"}]
        rewrite_fixtures(paths, sl_components=sl)
        self.svc.refresh(self.mid, "alice")
        job = self.sync.create_job(self.mid, dry_run=False, confirm=True)
        self.assertIn("stale", {i["status"] for i in job["items"]})

    def test_safety_rails(self):
        with self.assertRaises(ValueError) as ctx:
            self.sync.create_job(self.mid, dry_run=False)  # no confirm
        self.assertEqual(str(ctx.exception), "live synchronization requires confirm=true")
        with self.assertRaises(ValueError) as ctx:
            self.sync.create_job(self.mid, dry_run=True, component_ids=[999999])  # nothing approved in selection
        self.assertEqual(str(ctx.exception), "no component is approved for synchronization")
        self.db.execute("UPDATE connections SET last_test_ok=0 WHERE role='pl'")
        with self.assertRaises(ValueError) as ctx:
            self.sync.create_job(self.mid, dry_run=False, confirm=True)  # connection not verified
        self.assertEqual(str(ctx.exception),
                         "PL connection has not passed a connectivity test; synchronization stays disabled")
        self.db.execute("UPDATE connections SET last_test_ok=1 WHERE role='pl'")
        self.sync.max_items = 1
        with self.assertRaises(ValueError) as ctx:
            self.sync.create_job(self.mid, dry_run=False, confirm=True)  # exceeds safety limit
        self.assertEqual(str(ctx.exception), "3 items exceeds the 1-item safety limit; narrow the selection")


class ListJobsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.svc, self.db, self.mid = make_service(self.dir, *pair_components())
        self.review, self.sync = make_review(self.svc)
        item = next(i for i in self.review.list_components(self.mid)["items"] if i["discrepancy_count"])
        detail = self.review.detail(item["id"])
        for row in detail["comparison"]["copyrights"]:
            if row["kind"] in ("variant", "sl_only", "pl_only"):
                self.review.decide(item["id"], "copyright", row["key"], "approve_sl", reviewer="bob")
        self.review.set_review_state(item["id"], "approved_for_sync", "bob")

    def test_list_jobs_filters_orders_and_limits(self):
        first = self.sync.create_job(self.mid, dry_run=True, actor="bob")
        second = self.sync.create_job(self.mid, dry_run=True, actor="bob")
        self.assertEqual([j["id"] for j in self.sync.list_jobs(self.mid)], [second["id"], first["id"]])
        self.assertEqual([j["id"] for j in self.sync.list_jobs()], [second["id"], first["id"]])
        self.assertEqual([j["id"] for j in self.sync.list_jobs(self.mid, limit=1)], [second["id"]])
        self.assertEqual(self.sync.list_jobs(999), [])
        self.assertIs(self.sync.list_jobs(self.mid)[0]["dry_run"], True)


class InterruptedJobTests(unittest.TestCase):
    """A process that died mid-write must leave the job recoverable, not wedged."""

    def setUp(self):
        self.dir = tmpdir()
        self.pl, self.sl = components()
        self.svc, self.db, self.mid = make_service(self.dir, self.pl, self.sl)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        approve_all(self.review, self.match_id)

    def crash_after_first_write(self):
        """Run a live job and kill the process on the second write.

        A plain Exception out of the connector is not caught per-item by _execute, so it
        propagates out of create_job exactly as SIGKILL would: the first item is committed,
        the job stays 'in_progress', and the rest of the items are still 'ready'.
        """
        conn = self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"])
        writes = {"n": 0}
        reals = {name: getattr(conn, name)
                 for name in ("update_copyrights", "update_licenses", "update_license_text")}

        def gate(name):
            def wrapped(*a, **kw):
                writes["n"] += 1
                if writes["n"] == 2:
                    raise RuntimeError("process killed")
                return reals[name](*a, **kw)
            return wrapped

        with mock.patch.multiple(conn,
                               update_copyrights=gate("update_copyrights"),
                               update_licenses=gate("update_licenses"),
                               update_license_text=gate("update_license_text")):
            with self.assertRaises(RuntimeError):
                self.sync.create_job(self.mid, dry_run=False, confirm=True)
        self.assertEqual(writes["n"], 2, "fixture must have at least two writable items")
        return self.db.one("SELECT * FROM sync_jobs WHERE id=(SELECT MAX(id) FROM sync_jobs)")

    def test_a_crash_leaves_the_job_in_progress_with_unattempted_items(self):
        job = self.crash_after_first_write()
        self.assertEqual(job["status"], "in_progress")
        self.assertIsNone(job["finished_at"])
        rows = self.db.query("SELECT status FROM sync_items WHERE job_id=?", (job["id"],))
        self.assertIn("ready", {r["status"] for r in rows})

    def test_reconcile_marks_the_half_run_job_and_its_items_interrupted(self):
        job = self.crash_after_first_write()
        recovered = self.sync.reconcile_interrupted_jobs()
        self.assertEqual(recovered["jobs"], 1)
        self.assertGreaterEqual(recovered["items"], 1)
        row = self.db.one("SELECT * FROM sync_jobs WHERE id=?", (job["id"],))
        self.assertEqual(row["status"], "interrupted")
        self.assertIsNotNone(row["finished_at"])
        self.assertIn("interrupted", json.loads(row["summary_json"]))
        statuses = [i["status"] for i in self.sync.job(job["id"])["items"]]
        self.assertIn("succeeded", statuses)   # the write that landed stays landed
        self.assertIn("interrupted", statuses)  # the writes that never ran are visible

    def test_interrupted_items_can_be_retried_and_converge(self):
        job = self.crash_after_first_write()
        self.sync.reconcile_interrupted_jobs()
        retried = self.sync.retry(job["id"], confirm=True, actor="alice")
        self.assertEqual({i["status"] for i in retried["items"]}, {"succeeded"})
        self.assertEqual(retried["status"], "completed")

    def test_reconcile_leaves_a_settled_job_alone(self):
        done = self.sync.create_job(self.mid, dry_run=False, confirm=True)
        self.assertEqual(self.sync.reconcile_interrupted_jobs(), {"jobs": 0, "items": 0})
        self.assertEqual(self.db.one("SELECT status FROM sync_jobs WHERE id=?", (done["id"],))["status"],
                         done["status"])

    def test_reconcile_is_idempotent(self):
        job = self.crash_after_first_write()
        self.assertEqual(self.sync.reconcile_interrupted_jobs()["jobs"], 1)
        self.assertEqual(self.sync.reconcile_interrupted_jobs(), {"jobs": 0, "items": 0})
        self.assertEqual(self.db.one("SELECT status FROM sync_jobs WHERE id=?", (job["id"],))["status"],
                         "interrupted")

    def test_reconcile_leaves_dry_run_jobs_alone(self):
        dry = self.sync.create_job(self.mid, dry_run=True)
        self.db.execute("UPDATE sync_jobs SET status='pending' WHERE id=?", (dry["id"],))
        self.assertEqual(self.sync.reconcile_interrupted_jobs(), {"jobs": 0, "items": 0})
        self.assertEqual(self.db.one("SELECT status FROM sync_jobs WHERE id=?", (dry["id"],))["status"],
                         "pending")

    def test_reconcile_audits_the_recovery(self):
        self.crash_after_first_write()
        self.sync.reconcile_interrupted_jobs()
        events = self.svc.audit_events(self.mid, limit=50)
        self.assertIn("sync.job_reconciled", {e["action"] for e in events})

    def test_a_job_whose_writes_all_landed_is_not_called_interrupted(self):
        """Crash after the last write but before the job was finished.

        Nothing is left unwritten, so calling this 'interrupted' would send the reviewer
        hunting for writes that are already in the PL tool.
        """
        conn = self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"])
        reals = {name: getattr(conn, name)
                 for name in ("update_copyrights", "update_licenses", "update_license_text")}

        def gate(name):
            def wrapped(*a, **kw):
                return reals[name](*a, **kw)
            return wrapped

        def crash_before_reporting(job_id):
            # every write has landed and its item is terminal, but the process dies before
            # _finish ever runs — so the job is still 'in_progress' in the database
            raise RuntimeError("process killed before the job was reported")

        with mock.patch.multiple(conn,
                               update_copyrights=gate("update_copyrights"),
                               update_licenses=gate("update_licenses"),
                               update_license_text=gate("update_license_text")), \
             mock.patch.object(self.sync, "_finish", side_effect=crash_before_reporting):
            with self.assertRaises(RuntimeError):
                self.sync.create_job(self.mid, dry_run=False, confirm=True)

        job = self.db.one("SELECT * FROM sync_jobs WHERE id=(SELECT MAX(id) FROM sync_jobs)")
        self.assertEqual(job["status"], "in_progress")
        terminal = self.db.query("SELECT status FROM sync_items WHERE job_id=?", (job["id"],))
        self.assertTrue(terminal, "the job must have items")
        self.assertEqual({r["status"] for r in terminal}, {"succeeded"},
                        "every write must have landed before the crash, or this proves nothing")
        self.sync.reconcile_interrupted_jobs()
        row = self.db.one("SELECT * FROM sync_jobs WHERE id=?", (job["id"],))
        self.assertEqual(row["status"], "completed")
        self.assertNotIn("interrupted", json.loads(row["summary_json"]))
        # and there is nothing left to retry, which is the honest answer
        with self.assertRaises(ValueError) as ctx:
            self.sync.retry(job["id"], confirm=True)
        self.assertEqual(str(ctx.exception), "no failed or interrupted items to retry")


if __name__ == "__main__":
    unittest.main()
