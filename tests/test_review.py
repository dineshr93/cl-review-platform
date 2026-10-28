"""Review workflow tests: field-level decisions, statuses, rejections, staleness."""
from __future__ import annotations

import unittest

from clreview.sync import preview_match

from helpers import (GENERIC_ZLIB, SOURCE_ZLIB, make_review, make_service, paths_for,
                     pl_component, rewrite_fixtures, tmpdir)
from test_e2e import Server


def base_components():
    pl = [pl_component(
        name="zlib", version="1.2.11", purl="pkg:github/madler/zlib@1.2.11",
        licenses=[{"name": "Zlib", "spdx": "Zlib"}],
        copyrights=[{"text": "Copyright (C) 1995-2017 Jean-loup Gailly and Mark Adler", "origin": "origin"}],
        license_texts=[{"kind": "generic_metadata", "text": GENERIC_ZLIB, "provenance": "kb"}],
    )]
    sl = [{
        "external_id": "zlib-1.2.11", "name": "zlib", "version": "1.2.11",
        "purl": "pkg:github/madler/zlib@1.2.11",
        "licenses": [{"name": "Zlib", "spdx": "Zlib", "evidence_paths": ["zlib/LICENSE"]}],
        "copyrights": [{"text": "Copyright  1995-2017  Jean-loup Gailly and Mark Adler", "source_path": "zlib/zutil.c"}],
        "license_texts": [
            {"kind": "source_file", "file_name": "LICENSE", "file_path": "zlib/LICENSE",
             "is_license_file": True, "provenance": "fossology item content", "text": SOURCE_ZLIB},
            {"kind": "generic_metadata", "provenance": "fossology /license/Zlib", "text": GENERIC_ZLIB},
        ],
    }]
    return pl, sl


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.pl, self.sl = base_components()
        self.svc, self.db, self.mid = make_service(self.dir, self.pl, self.sl)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        self.detail = self.review.detail(self.match_id)

    def _decide_copyright(self):
        row = [r for r in self.detail["comparison"]["copyrights"] if r["kind"] == "variant"][0]
        return self.review.decide(self.match_id, "copyright", row["key"], "approve_sl", reviewer="alice")

    def _decide_text(self):
        pref = self.detail["comparison"]["license_text"]["preferred"]
        return self.review.decide(self.match_id, "license_text", pref["content_hash"], "approve_sl", reviewer="alice")

    def test_field_level_decisions_keep_component_in_progress(self):
        res = self._decide_copyright()
        self.assertEqual(res["review_status"], "in_progress")
        self.assertEqual(res["undecided"], 1)

    def test_all_fields_decided_reaches_reviewed(self):
        self._decide_copyright()
        res = self._decide_text()
        self.assertEqual(res["review_status"], "reviewed")

    def test_note_only_does_not_resolve_a_field(self):
        row = [r for r in self.detail["comparison"]["copyrights"] if r["kind"] == "variant"][0]
        self.review.decide(self.match_id, "copyright", row["key"], "note_only", note="checked upstream")
        self.assertEqual(self.review.match(self.match_id)["review_status"], "review_required")

    def test_approve_for_sync_requires_decisions(self):
        with self.assertRaises(ValueError) as ctx:
            self.review.set_review_state(self.match_id, "approved_for_sync")
        self.assertEqual(str(ctx.exception), "2 discrepant field(s) still undecided")
        self._decide_copyright()
        with self.assertRaises(ValueError) as ctx:
            self.review.set_review_state(self.match_id, "approved_for_sync")
        self.assertEqual(str(ctx.exception), "1 discrepant field(s) still undecided")
        self._decide_text()
        out = self.review.set_review_state(self.match_id, "approved_for_sync", "alice")
        self.assertEqual(out["review_status"], "approved_for_sync")
        decisions = self.db.query("SELECT * FROM decisions WHERE match_id=?", (self.match_id,))
        preview = preview_match(self.svc, self.review.match(self.match_id), decisions)
        self.assertTrue(preview)
        self.assertTrue(all(i["approved_for_sync"] for i in preview))

    def test_license_text_must_come_from_source_candidate(self):
        with self.assertRaises(ValueError):
            self.review.decide(self.match_id, "license_text", "not-a-candidate", "approve_sl")
        with self.assertRaises(ValueError):
            self.review.decide(self.match_id, "license_text", "x", "correct", approved_value="invented text")

    def test_notes_are_recorded(self):
        self.review.add_note(self.match_id, "copyright owner confirmed upstream", "alice")
        notes = self.review.detail(self.match_id)["notes"]
        self.assertEqual(notes[-1]["body"], "copyright owner confirmed upstream")
        self.assertEqual(notes[-1]["reviewer"], "alice")

    def test_rejected_suggestion_survives_refresh(self):
        self.dir2 = tmpdir()
        pl = [pl_component(name="libpng16", version="1.6.37")]
        sl = [{"external_id": "libpng-1.6.37", "name": "libpng", "version": "1.6.37"}]
        svc, db, mid = make_service(self.dir2, pl, sl)
        review, _ = make_review(svc)
        item = review.list_components(mid)["items"][0]
        self.assertEqual(item["status"], "suggested")
        review.reject_match(item["id"], "different component", "alice")
        svc.refresh(mid, "alice")
        after = review.list_components(mid)["items"][0]
        self.assertEqual(after["status"], "rejected")
        self.assertEqual(review.match(item["id"])["status"], "rejected")

    def test_stale_approval_after_sl_data_changes(self):
        self._decide_copyright()
        self._decide_text()
        self.review.set_review_state(self.match_id, "approved_for_sync", "alice")
        paths = paths_for(self.svc, self.mid)
        sl = self.sl
        sl[0]["copyrights"] = [{"text": "Copyright 2024 Someone Else", "source_path": "zlib/zutil.c"}]
        rewrite_fixtures(paths, sl_components=sl)
        self.svc.refresh(self.mid, "alice")
        detail = self.review.detail(self.match_id)
        self.assertTrue(detail["stale"])
        self.assertTrue(any("SL observations changed" in s["reason"] for s in detail["stale"]))

    def test_unmatched_component_cannot_be_approved(self):
        d = tmpdir()
        svc, db, mid = make_service(d, [pl_component(name="solo", version="1.0")], [])
        review, _ = make_review(svc)
        item = review.list_components(mid)["items"][0]
        self.assertEqual(item["status"], "unmatched")
        with self.assertRaises(ValueError) as ctx:
            review.set_review_state(item["id"], "approved_for_sync")
        self.assertEqual(str(ctx.exception), "no approved SL-derived correction to synchronize")


def pair_components():
    """Two per side: one matched pair, one orphan on each side, and a copyright variant to decide."""
    pl = [pl_component(name="zlib", version="1.2.11", purl="pkg:github/madler/zlib@1.2.11",
                       copyrights=[{"text": "Copyright (C) 1995-2017 Jean-loup Gailly", "origin": "origin"}]),
          pl_component(name="left-pad", version="1.0.0")]
    sl = [{"external_id": "zlib-1.2.11", "name": "zlib", "version": "1.2.11",
           "purl": "pkg:github/madler/zlib@1.2.11",
           "copyrights": [{"text": "Copyright  1995-2017  Jean-loup Gailly", "source_path": "zlib/zutil.c"}]},
          {"external_id": "other-2.0", "name": "other", "version": "2.0"}]
    return pl, sl


class ConfirmReassignTests(unittest.TestCase):
    """confirm_match and reassign are the two write paths with no test at all."""

    def setUp(self):
        self.dir = tmpdir()
        pl, sl = pair_components()
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.review, self.sync = make_review(self.svc)
        items = self.review.list_components(self.mid)["items"]
        self.matched = next(i for i in items if i["status"] != "unmatched")["id"]
        self.orphan = next(i for i in items if i["status"] == "unmatched" and i["pl_name"])["id"]

    def rows(self):
        return {r["id"]: r for r in self.db.query("SELECT * FROM matches WHERE mapping_id=?", (self.mid,))}

    def test_confirm_records_reviewer_and_time(self):
        out = self.review.confirm_match(self.matched, "alice")
        row = self.rows()[self.matched]
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["confirmed_by"], "alice")
        self.assertTrue(row["confirmed_at"])
        self.assertIn("review_status", out)                      # recompute ran as part of confirming
        actions = [r["action"] for r in self.db.query("SELECT * FROM audit WHERE entity_id=?", (self.matched,))]
        self.assertIn("match.confirmed", actions)

    def test_confirm_requires_both_sides(self):
        with self.assertRaises(ValueError) as ctx:
            self.review.confirm_match(self.orphan, "alice")
        self.assertEqual(str(ctx.exception), "both sides must be assigned before confirmation")
        self.assertNotEqual(self.rows()[self.orphan]["status"], "confirmed")

    def test_reassign_rewrites_the_pair_and_recomputes(self):
        out = self.review.reassign(self.matched, sl_key="other-2.0", reviewer="bob")
        row = self.rows()[self.matched]
        self.assertEqual((row["pl_key"], row["sl_key"]), ("zlib", "other-2.0"))
        self.assertEqual((row["sl_name"], row["sl_version"]), ("other", "2.0"))
        self.assertEqual((row["pl_name"], row["pl_version"]), ("zlib", "1.2.11"))
        self.assertEqual((row["method"], row["confidence"], row["status"]), ("manual", 1.0, "confirmed"))
        self.assertEqual(row["confirmed_by"], "bob")
        self.assertEqual(out["review_status"], "review_required")   # recomputed against the new pair
        audit = self.db.query("SELECT * FROM audit WHERE action='match.reassigned' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(audit["actor"], "bob")

    def test_reassign_rejects_a_key_that_is_not_in_the_snapshot(self):
        with self.assertRaises(ValueError) as ctx:
            self.review.reassign(self.matched, sl_key="does-not-exist")
        self.assertEqual(str(ctx.exception), "SL component 'does-not-exist' not in current snapshot")
        self.assertEqual(self.rows()[self.matched]["sl_key"], "zlib-1.2.11")
        with self.assertRaises(ValueError) as ctx:
            self.review.reassign(self.matched, pl_key="nope")
        self.assertEqual(str(ctx.exception), "PL component 'nope' not in current snapshot")

    def test_reassign_can_move_the_pl_side(self):
        self.review.reassign(self.matched, pl_key="left-pad", reviewer="bob")
        self.assertEqual(self.rows()[self.matched]["pl_name"], "left-pad")

    def test_overview_reports_sides_counts_and_recent_jobs(self):
        out = self.svc.overview(self.mid)
        self.assertEqual(out["mapping"]["id"], self.mid)
        for side in ("pl", "sl"):
            self.assertEqual(out["sides"][side]["status"], "ok")
            self.assertEqual(out["sides"][side]["count"], 2)
            self.assertTrue(out["sides"][side]["snapshot_id"])
        self.assertEqual(out["counts"]["matched"], 1)
        self.assertEqual(out["counts"]["unmatched"], 2)
        self.assertEqual(out["recent_sync"], [])
        self.db.execute("INSERT INTO sync_jobs(mapping_id,dry_run,status,created_by,created_at)"
                        " VALUES(?,1,'dry_run','bob',?)", (self.mid, "2026-01-01T00:00:00+00:00"))
        self.assertEqual([j["id"] for j in self.svc.overview(self.mid)["recent_sync"]], [1])


class DecisionValidationTests(unittest.TestCase):
    """Every refusal ``decide`` can raise, pinned verbatim - through the service and the route.

    These messages are what a reviewer reads in the toast when a decision is malformed, and no
    test pinned any of them before the value-resolution split.
    """

    CASES = (
        ({"field": "copyright", "entry_key": "deadbeef", "action": "approve_sl"},
         "approve_sl requires an SL copyright entry key"),
        ({"field": "copyright", "entry_key": "deadbeef", "action": "accept_pl"},
         "accept_pl requires a PL copyright entry key"),
        ({"field": "copyright", "entry_key": None, "action": "correct"},
         "correct requires approved_value"),
        ({"field": "copyright", "entry_key": "deadbeef", "action": "reject"},
         "entry_key does not exist in either side"),
        ({"field": "license_name", "entry_key": "deadbeef", "action": "approve_sl"},
         "approve_sl requires an SL license entry key"),
        ({"field": "license_name", "entry_key": "deadbeef", "action": "accept_pl"},
         "accept_pl requires a PL license entry key"),
        ({"field": "license_name", "entry_key": None, "action": "correct"},
         "correct requires approved_value"),
        ({"field": "license_name", "entry_key": "deadbeef", "action": "reject"},
         "entry_key does not exist in either side"),
        ({"field": "license_text", "entry_key": "deadbeef", "action": "approve_sl"},
         "license text must come from an identified SL source-file candidate"),
    )

    def setUp(self):
        self.dir = tmpdir()
        pl, sl = base_components()
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.review, _ = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_each_message_from_the_service(self):
        for body, message in self.CASES:
            with self.subTest(**body):
                with self.assertRaises(ValueError) as ctx:
                    self.review.decide(self.match_id, body["field"], body["entry_key"], body["action"],
                                       approved_value=body.get("approved_value"), reviewer="alice")
                self.assertEqual(str(ctx.exception), message)

    def test_each_message_over_http(self):
        for body, message in self.CASES:
            with self.subTest(**body):
                status, payload = self.server.call("POST", f"/api/components/{self.match_id}/decisions", body)
                self.assertEqual(status, 400, (body, payload))
                self.assertEqual(payload["error"], message)

    def test_note_only_requires_a_note(self):
        detail = self.review.detail(self.match_id)
        key = next(r["key"] for r in detail["comparison"]["copyrights"] if r["kind"] == "variant")
        with self.assertRaises(ValueError) as ctx:
            self.review.decide(self.match_id, "copyright", key, "note_only", reviewer="alice")
        self.assertEqual(str(ctx.exception), "note_only requires a note")
        status, payload = self.server.call("POST", f"/api/components/{self.match_id}/decisions",
                                           {"field": "copyright", "entry_key": key, "action": "note_only"})
        self.assertEqual((status, payload["error"]), (400, "note_only requires a note"))


class DetailWarningTests(unittest.TestCase):
    """The warning list is a contract with the workspace panel; extraction may not drop a rule."""

    def test_detail_warnings_are_unchanged(self):
        d = tmpdir()
        pl = [pl_component(name="libpng16", version="1.6.37")]
        sl = [{"external_id": "libpng-1.6.37", "name": "libpng", "version": "1.6.37"}]
        svc, db, mid = make_service(d, pl, sl)
        review, _ = make_review(svc)
        item = review.list_components(mid)["items"][0]
        self.assertEqual(review.detail(item["id"])["warnings"], ["fuzzy match requires confirmation"])

        rewrite_fixtures(paths_for(svc, mid), sl_components=[])
        svc.refresh(mid, "alice")
        self.assertEqual(review.detail(item["id"])["warnings"],
                         ["fuzzy match requires confirmation",
                          "one side missing from the current snapshot"])


if __name__ == "__main__":
    unittest.main()
