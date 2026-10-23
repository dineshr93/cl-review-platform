"""Review workflow tests: field-level decisions, statuses, rejections, staleness."""
from __future__ import annotations

import unittest

from clreview.sync import preview_match

from helpers import (GENERIC_ZLIB, SOURCE_ZLIB, fixture_set, make_review, make_service, paths_for,
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


if __name__ == "__main__":
    unittest.main()
