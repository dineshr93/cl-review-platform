import json
import tempfile
import threading
import time
import unittest
from unittest import mock

from clreview.api import Api
from clreview.connectors.base import ConnectorError, RefreshCancelled
from clreview.connectors.blackduck import BlackDuckConnector
from clreview.connectors.fossology import FossologyConnector
from clreview.domain import ComponentData
from clreview.jobs import RefreshJobs
from clreview.matching import SLScope
from clreview.service import now

from helpers import make_service, pl_component


class ConnectionAdminAndJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.db, self.mid = make_service(self.tmp.name, [], [])

    def test_edit_keeps_role_and_tool_and_resets_the_test_state(self):
        cid = self.svc.mapping(self.mid)["pl_connection_id"]
        fixtures = self.svc.connection(cid)["config"]["fixtures"]
        out = self.svc.update_connection(cid, "renamed", "", {"fixtures": fixtures}, {})
        self.assertEqual((out["name"], out["role"], out["tool"]), ("renamed", "pl", "mock-pl"))
        self.assertFalse(out["last_test_ok"])
        with self.assertRaises(ValueError):
            self.svc.update_connection(cid, "x", "", {"token": "secret"}, {})

    def test_delete_is_refused_while_a_mapping_uses_the_connection(self):
        cid = self.svc.mapping(self.mid)["pl_connection_id"]
        with self.assertRaisesRegex(ValueError, "test mapping"):
            self.svc.delete_connection(cid)
        spare = self.svc.add_connection("sl", "mock-sl", "spare", "", {}, {})
        self.assertEqual(self.svc.delete_connection(spare["id"]), {"deleted": spare["id"]})
        with self.assertRaises(KeyError):
            self.svc.connection(spare["id"])

    def test_background_refresh_finishes_and_can_be_cancelled(self):
        jobs = RefreshJobs(self.svc)
        job = jobs.start(self.mid, "tester")
        for _ in range(100):
            job = jobs.get(job["id"])
            if job["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "done")
        self.assertIn("sides", job["result"])

        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(RefreshCancelled):
            self.svc.refresh(self.mid, "tester", cancel=cancel)


class AtomicRefresh(unittest.TestCase):
    """A refresh stores both sides and the re-match together, or nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.db, self.mid = make_service(self.tmp.name, [pl_component(name="zlib", version="1")],
                                                   [pl_component(name="zlib", version="1")])
        self.snapshots = self._count("snapshots")

    def _count(self, table):
        return self.db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE mapping_id=?", (self.mid,))["n"]

    def test_cancel_after_both_sides_were_read_stores_nothing(self):
        cancel = threading.Event()
        sl = self.svc.connector_for(self.svc.mapping(self.mid)["sl_connection_id"])
        original = sl.fetch_inventory

        def read_then_cancel(*args):
            out = original(*args)
            cancel.set()  # the reviewer cancels after both sides were read, before the commit
            return out
        with mock.patch.object(sl, "fetch_inventory", side_effect=read_then_cancel):
            with self.assertRaises(RefreshCancelled):
                self.svc.refresh(self.mid, "tester", cancel=cancel)
        self.assertEqual(self._count("snapshots"), self.snapshots)

    def test_a_failure_while_saving_rolls_back_both_sides(self):
        with mock.patch.object(self.svc, "_run_matching", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.svc.refresh(self.mid, "tester")
        self.assertEqual(self._count("snapshots"), self.snapshots)

    def test_jobs_report_cancelling_at_once_and_refuse_a_cancel_while_saving(self):
        release = threading.Event()
        sl = self.svc.connector_for(self.svc.mapping(self.mid)["sl_connection_id"])
        original = sl.fetch_inventory
        jobs = RefreshJobs(self.svc)
        with mock.patch.object(sl, "fetch_inventory", side_effect=lambda *a: (release.wait(5), original(*a))[1]):
            job = jobs.start(self.mid, "tester")
            self.assertEqual(jobs.cancel(job["id"])["status"], "cancelling")
            self.assertEqual(jobs.running_for(self.mid), job["id"])
            release.set()
            for _ in range(200):
                if jobs.get(job["id"])["status"] == "cancelled":
                    break
                time.sleep(0.02)
        self.assertEqual(jobs.get(job["id"])["status"], "cancelled")
        self.assertIsNone(jobs.running_for(self.mid))
        self.assertEqual(self._count("snapshots"), self.snapshots)

        jobs._jobs[99] = {**jobs.get(job["id"]), "id": 99, "status": "running",
                          "progress": {"side": "commit", "done": 0, "total": None}}
        jobs._cancel[99] = threading.Event()
        jobs._clock[99] = {"job": time.monotonic(), "side": time.monotonic()}
        with self.assertRaisesRegex(ValueError, "can no longer be cancelled"):
            jobs.cancel(99)


class CancelIsPrompt(unittest.TestCase):
    """A cancel waits for at most the request in flight, not for a side to finish."""

    def test_folder_read_returns_on_cancel_while_uploads_are_still_in_flight(self):
        conn = FossologyConnector("https://fo.example/api/v1", {"workers": 2, "token": "t"})
        stuck = threading.Event()
        uploads = [{"id": i, "uploadname": f"lib-{i}-1.0", "uploaddate": "d"} for i in range(6)]
        cancel = threading.Event()
        outcome = {}

        def read():
            try:
                with conn.reporting(None, cancel):
                    conn.fetch_inventory("folder:7")
            except RefreshCancelled:
                outcome["cancelled"] = time.monotonic()
        with mock.patch.object(conn, "list_uploads", return_value=uploads), \
                mock.patch.object(conn, "_upload_summary", side_effect=lambda *a: stuck.wait(10)):
            worker = threading.Thread(target=read)
            worker.start()
            time.sleep(0.1)
            asked = time.monotonic()
            cancel.set()
            worker.join(2)
            stuck.set()
        self.assertIn("cancelled", outcome, "the folder read kept waiting for its in-flight uploads")
        self.assertLess(outcome["cancelled"] - asked, 1.0)

    def test_black_duck_checks_cancel_between_bom_components(self):
        conn = BlackDuckConnector("https://bd.example", {"token": "t"})
        cancel = threading.Event()
        raws = [{"componentName": f"c{i}", "componentVersionName": "1"} for i in range(3)]
        seen = []

        def copyrights(raw, cache):
            seen.append(raw)
            cancel.set()
            return []
        with mock.patch.object(conn, "_version_components", return_value=raws), \
                mock.patch.object(conn, "_licenses", return_value=([], [])), \
                mock.patch.object(conn, "_copyrights", side_effect=copyrights):
            with self.assertRaises(RefreshCancelled):
                with conn.reporting(None, cancel) as stats:
                    conn.fetch_inventory("p", "v")
        self.assertEqual((len(seen), stats["read"]), (1, 1))


class RefreshPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.db, self.mid = make_service(self.tmp.name, [], [])

    def test_a_refresh_with_history_and_no_counts_is_estimated_from_last_time(self):
        plan = self.svc.refresh_plan(self.mid)
        self.assertIsNotNone(plan["estimate_seconds"])
        self.assertFalse(plan["needs_approval"])

    def test_without_history_the_estimate_is_unknown_and_needs_approval(self):
        self.db.execute("DELETE FROM audit WHERE action='snapshot.refreshed'")
        plan = self.svc.refresh_plan(self.mid)
        self.assertIsNone(plan["estimate_seconds"])
        self.assertTrue(plan["needs_approval"])

    def test_the_estimate_scales_the_last_per_item_rate_to_the_items_left_to_read(self):
        self.db.audit("t", "snapshot.refreshed", "mapping", self.mid,
                      {"timing": {"sl": {"seconds": 10, "count": 5, "read": 5, "full": False}}}, now())
        sl = self.svc.connector_for(self.svc.mapping(self.mid)["sl_connection_id"])
        counted = {"items": 50, "unit": "uploads", "to_read": 40, "cached": 10, "work": "summaries"}
        with mock.patch.object(sl, "refresh_plan", create=True, side_effect=lambda *a, **k: counted):
            plan = self.svc.refresh_plan(self.mid)
        self.assertEqual(plan["sides"]["sl"]["estimate_seconds"], 80.0)
        self.assertTrue(plan["needs_approval"])


class DeleteMapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.db, self.mid = make_service(self.tmp.name, [pl_component(name="zlib", version="1")],
                                                   [pl_component(name="zlib", version="1")])
        m = self.svc.mapping(self.mid)
        self.other = self.svc.create_mapping("other", m["pl_connection_id"], "proj", "1.0",
                                             m["sl_connection_id"], "scan", "t")["id"]
        self.svc.refresh(self.other, "t")
        match = self.db.one("SELECT id FROM matches WHERE mapping_id=?", (self.mid,))["id"]
        self.db.execute("INSERT INTO notes(mapping_id,match_id,reviewer,body,created_at) VALUES(?,?,?,?,?)",
                        (self.mid, match, "t", "kept with the component", now()))

    def test_delete_removes_every_row_of_the_mapping_and_keeps_the_other(self):
        before_other = self.svc.mapping_usage(self.other)["counts"]
        with self.assertRaisesRegex(ValueError, "exact name"):
            self.svc.delete_mapping(self.mid, "test", "t")
        out = self.svc.delete_mapping(self.mid, "test mapping", "t")
        self.assertEqual(out["counts"]["notes"], 1)
        with self.assertRaises(KeyError):
            self.svc.mapping(self.mid)
        for table, where in self.svc._MAPPING_ROWS:
            left = self.db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", (self.mid,))["n"]
            self.assertEqual(left, 0, table)
        self.assertEqual(self.svc.mapping_usage(self.other)["counts"], before_other)
        audit = self.db.one("SELECT detail_json FROM audit WHERE action='mapping.deleted'")
        self.assertEqual(json.loads(audit["detail_json"])["name"], "test mapping")

    def test_the_api_refuses_to_delete_a_mapping_that_is_refreshing(self):
        api = Api(self.db)
        with mock.patch.object(api.jobs, "running_for", return_value=7):
            status, body = api.handle("DELETE", f"/api/mappings/{self.mid}", {"confirm": ["test mapping"]}, {}, "t")
        self.assertEqual(status, 409)
        self.assertIn("job 7", body["error"])
        self.svc.mapping(self.mid)


class ScopedRefresh(unittest.TestCase):
    """PL details are read only for what the SL side could match; the rest is listed, not stored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        pl = [pl_component(name="zlib", version="1.2.11"), pl_component(name="left-pad", version="1.0"),
              pl_component(name="Bouncy Castle", version="1.70")]
        self.svc, self.db, self.mid = make_service(self.tmp.name, pl, [pl_component(name="zlib", version="1.2.11")])

    def test_only_scoped_pl_components_are_stored_and_the_rest_are_listed(self):
        snap = self.svc.current_snapshot(self.mid, "pl")
        self.assertEqual([c["name"] for c in self.svc.snapshot_components(snap["id"])], ["zlib"])
        self.assertEqual([c["name"] for c in self.svc.listing_components(self.mid)], ["Bouncy Castle", "left-pad"])
        timing = self.svc._refresh_history(self.mid)["pl"]
        self.assertEqual((timing["listed"], timing["out_of_scope"]), (3, 2))

    def test_a_failed_sl_read_keeps_the_previous_pl_snapshot(self):
        before = self.svc.current_snapshot(self.mid, "pl")["id"]
        sl = self.svc.connector_for(self.svc.mapping(self.mid)["sl_connection_id"])
        pl = self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"])
        with mock.patch.object(sl, "fetch_inventory", side_effect=ConnectorError("FOSSology is down")), \
                mock.patch.object(pl, "fetch_inventory") as pl_read:
            out = self.svc.refresh(self.mid, "t")
        pl_read.assert_not_called()
        self.assertEqual(out["sides"]["pl"]["status"], "skipped")
        self.assertEqual(self.svc.current_snapshot(self.mid, "pl")["id"], before)

    def test_black_duck_reads_details_only_for_admitted_components(self):
        conn = BlackDuckConnector("https://bd.example", {"token": "t"})
        raws = [{"componentName": n, "componentVersionName": "1"} for n in ("zlib", "left-pad", "openssl")]
        scope = SLScope([ComponentData(external_id="s", name="zlib", version="1")])
        with mock.patch.object(conn, "_version_components", return_value=raws), \
                mock.patch.object(conn, "_licenses", return_value=([], [])), \
                mock.patch.object(conn, "_copyrights", return_value=[]) as detail:
            with conn.reporting(None, None) as stats:
                out = conn.fetch_inventory("p", "v", scope=scope)
        self.assertEqual([c.name for c in out], ["zlib"])
        self.assertEqual((detail.call_count, stats["listed"]), (1, 3))
        self.assertEqual([k for k, _, _ in scope.skipped], ["left-pad|1", "openssl|1"])


if __name__ == "__main__":
    unittest.main()
