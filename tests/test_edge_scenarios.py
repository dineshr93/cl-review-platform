"""C7: the failure, empty and racing paths no test took before.

Each test names a user-visible outcome — a snapshot row, an overview status, a queue that stays
empty, one decision row after two writers — not an internal call.
"""
from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from clreview.connectors.base import ConnectorError
from clreview.connectors.mock import MockPLConnector
from clreview.db import DB
from clreview.service import Service

from helpers import fixture_set, make_review, make_service, tmpdir
from test_connector_contract import bd_mod, blackduck, real_http
from test_e2e import Server
from test_review import pair_components

PL_DOWN = "PL is unreachable (503)"


def pl_unreachable(message: str = PL_DOWN):
    """Break the project-level connector at the boundary refresh() catches on."""
    return mock.patch.object(MockPLConnector, "fetch_inventory", side_effect=ConnectorError(message))


def bare_service():
    """A mapping that has never been refreshed: connections + mapping, no snapshots."""
    d = tmpdir()
    paths = fixture_set(d, [], [])
    db = DB(":memory:")
    svc = Service(db)
    pl = svc.add_connection("pl", "mock-pl", "PL", "", {"fixtures": paths["pl"]}, {})
    sl = svc.add_connection("sl", "mock-sl", "SL", "", {"fixtures": paths["sl"]}, {})
    svc.test_connection(pl["id"], "test")
    svc.test_connection(sl["id"], "test")
    mid = svc.create_mapping("edge", pl["id"], "proj", "1.0", sl["id"], "scan", "test")["id"]
    return svc, db, mid


class RefreshFailureTests(unittest.TestCase):
    """§3.1: a connector that fails mid-refresh is recorded, not swallowed."""

    def setUp(self):
        self.dir = tmpdir()
        pl, sl = pair_components()
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.addCleanup(self.db.close)
        self.rows = lambda: [dict(r) for r in
                             self.db.query("SELECT * FROM snapshots WHERE mapping_id=? ORDER BY id", (self.mid,))]

    def test_a_failing_side_stores_an_error_snapshot_and_keeps_the_good_one(self):
        good_pl = self.svc.current_snapshot(self.mid, "pl")["id"]
        with pl_unreachable():
            out = self.svc.refresh(self.mid, "alice")

        self.assertEqual(out["sides"]["pl"]["status"], "error")
        self.assertEqual(out["sides"]["pl"]["error"], PL_DOWN)
        self.assertEqual(out["sides"]["sl"]["status"], "ok")

        errors = [r for r in self.rows() if r["status"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual((errors[0]["side"], errors[0]["error"]), ("pl", PL_DOWN))
        # The last known state survives: the failed read must not erase the previous snapshot.
        self.assertEqual([r["id"] for r in self.rows() if r["status"] == "ok" and r["side"] == "pl"], [good_pl])
        self.assertEqual(self.svc.current_snapshot(self.mid, "pl")["id"], good_pl)
        self.assertEqual(out["matching"]["status"], "ok")   # matching ran against the last good snapshot

    def test_a_refresh_failure_is_what_the_audit_trail_shows(self):
        with pl_unreachable():
            self.svc.refresh(self.mid, "alice")
        row = self.db.query("SELECT * FROM audit WHERE action='snapshot.refreshed' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(json.loads(row["detail_json"])["sides"], {"pl": "error", "sl": "ok"})


class OverviewStatusTests(unittest.TestCase):
    """§3.2: the side panel says `never` before the first read and `error` after a failed one."""

    def setUp(self):
        self.svc, self.db, self.mid = bare_service()
        self.addCleanup(self.db.close)

    def test_a_mapping_never_refreshed_reports_never_for_both_sides(self):
        out = self.svc.overview(self.mid)
        for side in ("pl", "sl"):
            self.assertEqual(out["sides"][side]["status"], "never", side)
            self.assertIsNone(out["sides"][side]["error"], side)
            self.assertIsNone(out["sides"][side]["snapshot_id"], side)
            self.assertEqual(out["sides"][side]["count"], 0, side)

    def test_a_failed_refresh_is_shown_per_side_with_its_message(self):
        with pl_unreachable():
            self.svc.refresh(self.mid, "alice")
        out = self.svc.overview(self.mid)
        self.assertEqual(out["sides"]["pl"]["status"], "error")
        self.assertEqual(out["sides"]["pl"]["error"], PL_DOWN)
        self.assertEqual(out["sides"]["sl"]["status"], "ok")
        self.assertIsNone(out["sides"]["sl"]["error"])


class PartialMatchingTests(unittest.TestCase):
    """§3.3: matching refuses to invent a comparison when one side has no successful snapshot."""

    def setUp(self):
        self.svc, self.db, self.mid = bare_service()
        self.addCleanup(self.db.close)

    def test_the_first_refresh_failing_leaves_that_side_without_anything_to_match(self):
        with pl_unreachable():
            out = self.svc.refresh(self.mid, "alice")
        self.assertEqual(out["matching"]["status"], "partial")
        self.assertEqual(out["matching"]["reason"], "one side has no successful snapshot")
        self.assertEqual(out["matching"]["pl"], 0)
        self.assertEqual(self.review_items(), [])

    def review_items(self):
        from clreview.review import ReviewService
        return ReviewService(self.svc).list_components(self.mid)["items"]


class EmptyInventoryTests(unittest.TestCase):
    """§3.4: two empty inventories are a clean empty state, not an error and not a crash."""

    def setUp(self):
        self.dir = tmpdir()
        self.svc, self.db, self.mid = make_service(self.dir, [], [])
        self.addCleanup(self.db.close)
        self.review, self.sync = make_review(self.svc)

    def test_refresh_reports_zero_and_matching_proposes_nothing(self):
        out = self.svc.refresh(self.mid, "alice")
        self.assertEqual((out["sides"]["pl"]["count"], out["sides"]["sl"]["count"]), (0, 0))
        self.assertEqual(out["matching"]["status"], "ok")
        self.assertEqual((out["matching"]["pl"], out["matching"]["sl"], out["matching"]["proposed"]), (0, 0, 0))

    def test_the_queue_is_empty(self):
        self.assertEqual(self.review.list_components(self.mid)["items"], [])

    def test_a_sync_job_refuses_with_the_approval_message(self):
        with self.assertRaises(ValueError) as ctx:
            self.sync.create_job(self.mid, dry_run=True)
        self.assertEqual(str(ctx.exception), "no component is approved for synchronization")


class EmptyAuditTests(unittest.TestCase):
    """§3.5: an audit list with nothing in it is an empty list, not a 500."""

    def test_a_fresh_database_answers_200_with_no_items(self):
        db = DB(":memory:")
        server = Server(db)
        self.addCleanup(server.close)
        self.addCleanup(db.close)
        status, body = server.call("GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])

    def test_a_mapping_with_no_events_filters_to_nothing(self):
        svc, db, _ = bare_service()
        self.addCleanup(db.close)
        self.assertEqual(svc.audit_events(999999), [])


class DecisionUpsertTests(unittest.TestCase):
    """§3.6: deciding the same field twice replaces the decision; the history does not double-count.

    review.decide inserts with `ON CONFLICT(match_id,field,entry_key) DO UPDATE SET ...`, so the
    second decision overwrites the first in place rather than appending a row.
    """

    def setUp(self):
        self.dir = tmpdir()
        pl, sl = pair_components()
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.addCleanup(self.db.close)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]

    def rows(self):
        return self.db.query("SELECT * FROM decisions WHERE match_id=? AND field=? AND entry_key=?",
                             (self.match_id, "copyright", "c"))

    def test_the_second_decision_is_the_only_decision(self):
        self.review.decide(self.match_id, "copyright", "c", "correct",
                           approved_value="Copyright First", reviewer="alice")
        self.review.decide(self.match_id, "copyright", "c", "correct",
                           approved_value="Copyright Second", reviewer="bob")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["approved_value"], "Copyright Second")
        self.assertEqual(rows[0]["reviewer"], "bob")

    def test_a_different_entry_key_is_a_different_decision(self):
        self.review.decide(self.match_id, "copyright", "c", "correct",
                           approved_value="Copyright First", reviewer="alice")
        self.review.decide(self.match_id, "copyright", "d", "correct",
                           approved_value="Copyright Other", reviewer="alice")
        self.assertEqual(len(self.db.query("SELECT * FROM decisions WHERE match_id=?", (self.match_id,))), 2)


class BlackDuckDuplicateRowTests(unittest.TestCase):
    """§3.7: a BOM that lists the same component twice keeps both rows, distinguished by href.

    blackduck.fetch_inventory counts each `name|version` and, for a repeat, appends the tail of
    that row's `_meta.href` to the external id. Collapsing them would drop one origin's copyrights
    and one writable handle, so both survive as separate components.
    """

    def inventory(self):
        rows = [{"componentName": "lib", "componentVersionName": "1.0",
                 "_meta": {"href": "https://bd/api/components/aaa"}},
                {"componentName": "lib", "componentVersionName": "1.0",
                 "_meta": {"href": "https://bd/api/components/bbb"}}]

        def transport(method, url, headers, body, timeout):
            if "/components" in url:
                return 200, {}, json.dumps(rows).encode()
            return 200, {}, b"{}"

        with real_http(bd_mod, transport):
            return blackduck().fetch_inventory("p", "v")

    def test_two_rows_with_one_name_are_two_components(self):
        inventory = self.inventory()
        self.assertEqual([c.external_id for c in inventory], ["lib|1.0", "lib|1.0#bbb"])

    def test_each_duplicate_keeps_its_own_writable_handle(self):
        self.assertEqual([c.write_key for c in self.inventory()],
                         ["https://bd/api/components/aaa", "https://bd/api/components/bbb"])


class HttpRaceTests(unittest.TestCase):
    """§3.8: two reviewers deciding the same field at once leave one decision, not two and not none."""

    def setUp(self):
        self.dir = tmpdir()
        pl, sl = pair_components()
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.addCleanup(self.db.close)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        self.server = Server(self.db)
        self.addCleanup(self.server.close)

    def test_concurrent_decisions_on_one_field_settle_on_one_row(self):
        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def decide(value: str, reviewer: str) -> None:
            status, body = self.server.call(
                "POST", f"/api/components/{self.match_id}/decisions",
                {"field": "copyright", "entry_key": "c", "action": "correct",
                 "approved_value": value, "note": "race"}, reviewer=reviewer)
            with lock:
                results.append((status, body))

        threads = [threading.Thread(target=decide, args=(f"Copyright {i}", f"reviewer-{i}")) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual([s for s, _ in results], [200, 200])
        rows = self.db.query("SELECT * FROM decisions WHERE match_id=? AND field='copyright' AND entry_key='c'",
                             (self.match_id,))
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["approved_value"], ("Copyright 1", "Copyright 2"))
        self.assertEqual(rows[0]["reviewer"], f"reviewer-{rows[0]['approved_value'][-1]}")


if __name__ == "__main__":
    unittest.main()
