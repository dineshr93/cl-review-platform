"""Synchronization tests: dry-run, writes, verification, idempotency, limits, retries."""
from __future__ import annotations

import unittest

from clreview.domain import sha256

from helpers import (GENERIC_ZLIB, SOURCE_ZLIB, make_review, make_service, paths_for, pl_component,
                     register_readonly_pl, rewrite_fixtures, tmpdir)


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
        with self.assertRaises(ValueError):
            self.sync.create_job(self.mid, dry_run=False)  # no confirm
        with self.assertRaises(ValueError):
            self.sync.create_job(self.mid, dry_run=True, component_ids=[999999])  # nothing approved in selection
        self.db.execute("UPDATE connections SET last_test_ok=0 WHERE role='pl'")
        with self.assertRaises(ValueError):
            self.sync.create_job(self.mid, dry_run=False, confirm=True)  # connection not verified
        self.db.execute("UPDATE connections SET last_test_ok=1 WHERE role='pl'")
        self.sync.max_items = 1
        with self.assertRaises(ValueError):
            self.sync.create_job(self.mid, dry_run=False, confirm=True)  # exceeds safety limit


if __name__ == "__main__":
    unittest.main()
