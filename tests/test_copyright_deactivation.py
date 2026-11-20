"""A rejected PL-only copyright notice is deactivated in the PL tool by the sync, never deleted."""
from __future__ import annotations

import unittest

from clreview.domain import copyright_key
from clreview.undo import UndoService

from helpers import make_review, make_service, pl_component, tmpdir

PL_NOTICE, SL_NOTICE = "© 2025 Borewit", "Copyright © 2025 Borewit"


class CopyrightDeactivation(unittest.TestCase):
    def setUp(self):
        pl = [pl_component(name="codec", version="0.2.2", copyrights=[{"text": PL_NOTICE}])]
        sl = [{"external_id": "codec-0.2.2", "name": "codec", "version": "0.2.2", "copyrights": [{"text": SL_NOTICE}]}]
        self.svc, self.db, self.mid = make_service(tmpdir(), pl, sl)
        self.review, self.sync = make_review(self.svc)
        self.cid = self.review.list_components(self.mid)["items"][0]["id"]
        self.pl = self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"])

    def decide_and_sync(self, pl_action="reject"):
        self.review.decide(self.cid, "copyright", copyright_key(SL_NOTICE), "approve_sl", reviewer="alice")
        self.review.decide(self.cid, "copyright", copyright_key(PL_NOTICE), pl_action, reviewer="alice")
        self.review.set_review_state(self.cid, "approved_for_sync", "alice")
        return self.sync.create_job(self.mid, dry_run=False, confirm=True, finalize=True, actor="alice")

    def test_rejecting_a_pl_only_notice_deactivates_it_and_a_second_sync_has_nothing_to_do(self):
        job = self.decide_and_sync()
        items = {i["field"]: i for i in job["items"]}
        self.assertEqual(items["copyright_deactivate"]["status"], "succeeded")
        self.assertEqual(items["review_status"]["status"], "succeeded")
        self.assertEqual(self.pl.read_component("proj|1.0|codec")["copyrights"], [SL_NOTICE])

        self.svc.refresh(self.mid, "test")  # the snapshot now holds the notice as inactive
        again = self.sync.preview(self.mid)
        deact = [i for c in again["components"] for i in c["items"] if i["field"] == "copyright_deactivate"]
        self.assertEqual([i["status"] for i in deact], ["already_applied"])

    def test_keeping_the_pl_notice_writes_no_deactivation(self):
        job = self.decide_and_sync(pl_action="accept_pl")
        self.assertNotIn("copyright_deactivate", {i["field"] for i in job["items"]})
        self.assertIn(PL_NOTICE, self.pl.read_component("proj|1.0|codec")["copyrights"])

    def test_undo_reactivates_the_notice_and_deactivates_the_added_one(self):
        job = self.decide_and_sync()
        UndoService(self.sync).undo(job["id"], confirm=True, actor="alice")
        self.assertEqual(self.pl.read_component("proj|1.0|codec")["copyrights"], [PL_NOTICE])

    def test_a_component_synced_before_the_fix_is_fixed_by_approving_and_applying_again(self):
        self.decide_and_sync()
        # what the earlier sync left behind: the SL notice added, the rejected PL notice still active
        self.pl.set_copyright_active("proj|1.0|codec", PL_NOTICE, True)
        self.svc.refresh(self.mid, "test")
        self.review.set_review_state(self.cid, "approved_for_sync", "alice")
        out = self.sync.apply_component(self.cid, confirm=True, actor="alice")
        got = {i["field"]: i["status"] for i in out["job"]["items"]}
        self.assertEqual(got["copyright_deactivate"], "succeeded")
        self.assertEqual(got["copyright"], "skipped_already_applied")
        self.assertTrue(out["reviewed"])
        self.assertEqual(self.pl.read_component("proj|1.0|codec")["copyrights"], [SL_NOTICE])


if __name__ == "__main__":
    unittest.main()
