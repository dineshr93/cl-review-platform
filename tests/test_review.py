"""Review workflow tests: field-level decisions, statuses, rejections, staleness."""
from __future__ import annotations

import unittest

from clreview.sync import preview_match

from helpers import (GENERIC_ZLIB, SOURCE_ZLIB, make_review, make_service, paths_for,
                     pl_component, rewrite_fixtures, tmpdir)


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
        with self.assertRaises(ValueError):
            self.review.set_review_state(self.match_id, "approved_for_sync")
        self._decide_copyright()
        with self.assertRaises(ValueError):
            self.review.set_review_state(self.match_id, "approved_for_sync")
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
        with self.assertRaises(ValueError):
            review.set_review_state(item["id"], "approved_for_sync")


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
        self.assertIn("both sides", str(ctx.exception))
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
        self.assertIn("not in current snapshot", str(ctx.exception))
        self.assertEqual(self.rows()[self.matched]["sl_key"], "zlib-1.2.11")
        with self.assertRaises(ValueError):
            self.review.reassign(self.matched, pl_key="nope")

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


if __name__ == "__main__":
    unittest.main()
