"""Read-path scale: cost follows the page, the queue and the job - never the whole inventory.

Every assertion here is a count (statements, rows, commits), never milliseconds: a statement
count says exactly what changed and does not flap on a loaded CI box.
"""
from __future__ import annotations

import unittest
from collections import Counter
from typing import Any

from clreview.db import DB
from clreview.domain import copyright_key
from clreview.review import ReviewService
from clreview.service import SNAPSHOTS_PER_SIDE, Service
from clreview.sync import SyncService

from helpers import fixture_set, pl_component, tmpdir


class Counted:
    """Count every statement that passes through a DB while the block runs.

    `rows_where('FROM components')` is how page-scoping is pinned: it sums the rows returned by
    the statements containing that fragment, so an assertion names the query that must have shrunk.
    """

    def __init__(self, db: DB):
        self.db = db
        self.counts: Counter = Counter()
        self.rows: list[tuple[str, int]] = []
        self._original: dict[str, Any] = {}

    def __enter__(self) -> "Counted":
        for name in ("query", "one", "execute"):
            fn = getattr(self.db, name)
            self._original[name] = fn
            setattr(self.db, name, self._wrap(name, fn))
        return self

    def __exit__(self, *exc) -> bool:
        for name, fn in self._original.items():
            setattr(self.db, name, fn)
        return False

    @staticmethod
    def _shape(sql: str) -> str:
        words = " ".join(sql.split()).split(" ")
        head = words[0].upper()
        if head == "SELECT":
            return "SELECT " + sql.split("FROM")[1].split()[0]
        if head == "UPDATE":
            return "UPDATE " + words[1]
        return head

    def _wrap(self, kind: str, fn):
        def inner(sql, params=()):
            self.counts["TOTAL"] += 1
            self.counts[self._shape(sql)] += 1
            out = fn(sql, params)
            n = len(out) if kind == "query" and isinstance(out, list) else (1 if out else 0)
            self.counts["ROWS"] += n
            self.rows.append((sql, n))
            return out
        return inner

    def rows_where(self, needle: str) -> int:
        low = needle.lower()
        return sum(n for sql, n in self.rows if low in sql.lower())


def components(n: int, *, sl_extra: bool = False) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        name = "lib%d" % i
        copyrights = [{"text": "Copyright (C) 2020 Owner %d" % i, "origin": "kb"}]
        if sl_extra:
            copyrights.append({"text": "Copyright 2024 SL %d" % i})
        out.append(pl_component(name=name, version="1.0", purl="pkg:gen/%s@1.0" % name,
                                licenses=[{"name": "MIT", "spdx": "MIT"}], copyrights=copyrights))
    return out


class ScaleFixture(unittest.TestCase):
    """Build a real service over N components with writable mock connectors."""

    def build(self, n: int = 40, approve: bool = False) -> dict[str, Any]:
        self.dir = tmpdir()
        self.paths = fixture_set(self.dir, components(n), components(n, sl_extra=True))
        self.db = DB(":memory:")
        self.addCleanup(self.db.close)
        self.svc = Service(self.db)
        pl = self.svc.add_connection("pl", "mock-pl", "PL", "", {"fixtures": self.paths["pl"]}, {})
        sl = self.svc.add_connection("sl", "mock-sl", "SL", "", {"fixtures": self.paths["sl"]}, {})
        self.svc.test_connection(pl["id"], "test")
        self.svc.test_connection(sl["id"], "test")
        self.mid = self.svc.create_mapping("scale", pl["id"], "proj", "1.0", sl["id"], "scan", "test")["id"]
        self.svc.refresh(self.mid, "test")
        self.review = ReviewService(self.svc)
        self.sync = SyncService(self.svc)
        if approve:
            for item in self.review.list_components(self.mid, limit=10_000)["items"]:
                index = int(item["pl_name"][3:])
                self.review.decide(item["id"], "copyright", copyright_key("Copyright 2024 SL %d" % index),
                                   "approve_sl", reviewer="t")
                self.review.set_review_state(item["id"], "approved_for_sync", "t")
        return {"svc": self.svc, "db": self.db, "mid": self.mid}


class PreviewFanoutTests(ScaleFixture):
    def test_preview_statements_do_not_grow_with_the_queue(self):
        def measure(n: int) -> int:
            self.build(n, approve=True)
            with Counted(self.db) as c:
                out = self.sync.preview(self.mid)
            self.assertEqual(len(out["components"]), n)
            return c.counts["TOTAL"]

        small, big = measure(40), measure(400)
        self.assertLessEqual(big - small, 8, f"40 components: {small} statements, 400: {big}")

    def test_preview_still_reports_the_approved_changes(self):
        self.build(5, approve=True)
        out = self.sync.preview(self.mid)
        self.assertEqual(len(out["components"]), 5)
        for comp in out["components"]:
            index = int(comp["match"]["pl_name"][3:])
            self.assertTrue(comp["match"]["approved_for_sync"])
            self.assertEqual([i["field"] for i in comp["items"]], ["copyright"])
            self.assertEqual([i["approved_value"] for i in comp["items"]], ["Copyright 2024 SL %d" % index])
            self.assertEqual([i["status"] for i in comp["items"]], ["ready"])


class QueueListingTests(ScaleFixture):
    def test_the_page_reads_only_its_own_components(self):
        def measure(n: int) -> tuple[Counter, list[tuple[str, int]]]:
            self.build(n)
            with Counted(self.db) as c:
                page = self.review.list_components(self.mid, limit=50)
            self.assertEqual(len(page["items"]), 50)
            return c.counts, c.rows

        small, small_rows = measure(80)
        big, big_rows = measure(800)
        self.assertLessEqual(big["TOTAL"] - small["TOTAL"], 8, "a bigger inventory must not cost more reads")
        lic = sum(n for sql, n in big_rows if "from licenses" in sql.lower())
        self.assertLessEqual(lic, 2 * 50, "license rows are read for the page, not the inventory")
        scoped = sum(n for sql, n in big_rows
                     if "from components" in sql.lower() and "external_id in" in sql.lower())
        self.assertLessEqual(scoped, 2 * 50)
        whole = sum(1 for sql, _ in big_rows if sql.lstrip().upper().startswith("SELECT * FROM components"))
        self.assertEqual(whole, 0, "the queue must never read every component row of the snapshot")

    def test_search_still_reaches_beyond_the_page(self):
        self.build(300)
        by_name = self.review.list_components(self.mid, q="lib287")
        self.assertEqual([i["pl_name"] for i in by_name["items"]], ["lib287"])
        by_purl = self.review.list_components(self.mid, q="pkg:gen/lib287@1.0")
        self.assertEqual(by_purl["total"], 1)
        by_license = self.review.list_components(self.mid, q="MIT", limit=50)
        self.assertEqual(by_license["total"], 300)

    def test_row_content_survives_the_page_scoped_read(self):
        self.build(60)
        page = self.review.list_components(self.mid, limit=5)
        row = page["items"][0]
        self.assertEqual(row["licenses"], ["MIT"])
        self.assertEqual(row["method"], "purl")     # both sides carry the same purl
        self.assertEqual(row["review_status"], "review_required")   # the sides do differ
        self.assertIsInstance(row["mismatch_reasons"], list)
        self.assertEqual(sorted(i["id"] for i in page["items"]), [1, 2, 3, 4, 5])


class ConnectionListingTests(ScaleFixture):
    def test_listing_mappings_reads_connections_once(self):
        self.build(2)
        for i in range(9):
            self.db.execute(
                "INSERT INTO mappings(name,pl_connection_id,sl_connection_id,pl_project,pl_version,sl_scan,created_at)"
                " VALUES(?,?,?,?,?,?,?)", (f"m{i}", 1, 2, "proj", "1.0", "scan", "2026-01-01T00:00:00+00:00"))
        with Counted(self.db) as c:
            rows = self.svc.list_mappings()
        self.assertEqual(len(rows), 10)
        self.assertLessEqual(c.counts["TOTAL"], 4, dict(c.counts))
        self.assertEqual(c.counts["SELECT connections"], 1)
        self.assertTrue(all(r["pl_connection"]["tool"] == "mock-pl" for r in rows))


class JobListingTests(ScaleFixture):
    def test_list_jobs_reads_scale_with_jobs_not_items(self):
        def measure(n: int) -> int:
            self.build(n, approve=True)
            self.sync.create_job(self.mid, dry_run=True)
            with Counted(self.db) as c:
                jobs = self.sync.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(len(jobs[0]["items"]), n)
            return c.counts["TOTAL"]

        small, big = measure(20), measure(200)
        self.assertLessEqual(big - small, 4, f"20 items: {small}, 200 items: {big}")

    def test_job_content_is_unchanged_by_the_batched_read(self):
        self.build(6, approve=True)
        job = self.sync.create_job(self.mid, dry_run=True)
        listed = self.sync.list_jobs()[0]
        self.assertEqual(listed, job)
        self.assertEqual(len(job["components"]), 6)
        self.assertEqual(sum(len(c["items"]) for c in job["components"]), len(job["items"]))
        self.assertTrue(all(set(c["counts"]) <= {"dry_run", "ready", "already_applied"}
                            for c in job["components"]))


class HistoryGrowthTests(ScaleFixture):
    def test_refresh_stops_copying_history_forever(self):
        self.build(10)
        for _ in range(9):
            self.svc.refresh(self.mid, "test")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM snapshots")["n"], 2 * SNAPSHOTS_PER_SIDE)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM components")["n"], 2 * SNAPSHOTS_PER_SIDE * 10)

    def test_the_previous_snapshot_survives_the_prune(self):
        self.build(4)
        for _ in range(9):
            self.svc.refresh(self.mid, "test")
        self.assertTrue(self.svc.snapshot_versions(self.mid, "pl", previous=True))

    def test_reviewer_history_outlives_the_prune(self):
        self.build(4, approve=True)
        match_id = self.review.list_components(self.mid)["items"][0]["id"]
        for _ in range(9):
            self.svc.refresh(self.mid, "test")
        rows = self.db.query("SELECT * FROM decisions WHERE match_id=?", (match_id,))
        self.assertEqual([r["action"] for r in rows], ["approve_sl"])
        detail = self.review.detail(match_id)
        self.assertEqual(detail["match"]["review_state"], "approved_for_sync")
        self.assertIn("Copyright 2024 SL 0",
                      [r["sl_text"] for r in detail["comparison"]["copyrights"] if r["decision"]])

    def test_matches_for_components_that_vanished_are_dropped(self):
        self.build(6)
        self.review.decide(1, "copyright", copyright_key("Copyright 2024 SL 0"), "approve_sl", reviewer="t")
        fixture_set(self.dir, components(1), components(1))   # five components are gone from both sides
        out = self.svc.refresh(self.mid, "test")
        self.assertEqual(out["matching"]["matches_pruned"], 5)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM matches")["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM decisions")["n"], 1)

    def test_a_stale_match_is_kept_because_it_is_a_finding(self):
        self.build(6)
        fixture_set(self.dir, components(6), components(1))   # five components lost on the SL side only
        out = self.svc.refresh(self.mid, "test")
        self.assertEqual(out["matching"]["matches_pruned"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM matches WHERE id <= 6")["n"], 6)


class CommitDisciplineTests(ScaleFixture):
    def commits_of(self, fn) -> int:
        before = self.db.commits
        fn()
        return self.db.commits - before

    def one_match(self) -> int:
        return self.review.list_components(self.mid)["items"][0]["id"]

    def test_a_decision_is_one_commit(self):
        self.build(4)
        mid = self.one_match()
        self.assertEqual(self.commits_of(lambda: self.review.decide(
            mid, "copyright", copyright_key("Copyright 2024 SL 0"), "approve_sl", reviewer="t")), 1)

    def test_review_state_and_recompute_are_one_commit(self):
        self.build(4)
        mid = self.one_match()
        self.review.decide(mid, "copyright", copyright_key("Copyright 2024 SL 0"), "approve_sl", reviewer="t")
        self.assertEqual(self.commits_of(
            lambda: self.review.set_review_state(mid, "approved_for_sync", "t")), 1)

    def test_a_note_and_its_audit_row_are_one_commit(self):
        self.build(4)
        mid = self.one_match()
        self.assertEqual(self.commits_of(lambda: self.review.add_note(mid, "checked", "t")), 1)

    def test_confirmation_is_one_commit(self):
        self.build(4)
        mid = self.one_match()
        self.assertEqual(self.commits_of(lambda: self.review.confirm_match(mid, "t")), 1)

    def test_a_suggestion_and_its_audit_row_are_one_commit(self):
        self.build(4)
        mid = self.one_match()
        self.assertEqual(self.commits_of(lambda: self.review.add_suggestion(
            mid, "license_name", "MIT", "MIT", confidence=0.9, model="test")), 1)

    def test_a_sync_job_is_one_commit(self):
        self.build(4, approve=True)
        self.assertEqual(self.commits_of(lambda: self.sync.create_job(self.mid, dry_run=True)), 1)

    def test_a_refresh_commits_its_phases_not_its_rows(self):
        self.build(40)
        self.assertLessEqual(self.commits_of(lambda: self.svc.refresh(self.mid, "test")), 6)


if __name__ == "__main__":
    unittest.main()
