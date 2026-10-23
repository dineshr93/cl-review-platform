"""LLM-readiness: machine suggestions stay separate from human decisions."""
from __future__ import annotations

import unittest

from helpers import GENERIC_ZLIB, SOURCE_ZLIB, make_review, make_service, pl_component, tmpdir


def components():
    pl = [pl_component(name="zlib", version="1.2.11",
                       licenses=[{"name": "Zlib", "spdx": "Zlib"}],
                       copyrights=[{"text": "Copyright (C) 1995-2017 Jean-loup Gailly", "origin": "origin"}],
                       license_texts=[{"kind": "generic_metadata", "text": GENERIC_ZLIB, "provenance": "kb"}])]
    sl = [{"external_id": "zlib-1.2.11", "name": "zlib", "version": "1.2.11",
           "licenses": [{"name": "Zlib", "spdx": "Zlib"}],
           "copyrights": [{"text": "Copyright  1995-2017  Jean-loup Gailly"}],
           "license_texts": [{"kind": "source_file", "file_name": "LICENSE", "file_path": "zlib/LICENSE",
                              "is_license_file": True, "provenance": "fossology item content", "text": SOURCE_ZLIB}]}]
    return pl, sl


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.db, self.mid = make_service(tmpdir(), *components())
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        self.detail = self.review.detail(self.match_id)

    def test_open_suggestion_never_synchronizes(self):
        row = [r for r in self.detail["comparison"]["copyrights"] if r["kind"] == "variant"][0]
        self.review.add_suggestion(self.match_id, "copyright", row["key"], row["sl_text"],
                                   confidence=0.81, model="test-model@1", evidence={"reason": "near-identical"})
        with self.assertRaises(ValueError):
            self.review.set_review_state(self.match_id, "approved_for_sync")
        with self.assertRaises(ValueError):
            self.sync.create_job(self.mid, dry_run=True)

    def test_accepting_suggestion_creates_a_human_decision(self):
        row = [r for r in self.detail["comparison"]["copyrights"] if r["kind"] == "variant"][0]
        sid = self.review.add_suggestion(self.match_id, "copyright", row["key"], row["sl_text"],
                                         confidence=0.81, model="test-model@1")["id"]
        out = self.review.decide_suggestion(sid, "accept", "alice")
        self.assertEqual(out["status"], "accepted")
        decision = self.db.one("SELECT * FROM decisions WHERE match_id=? AND field='copyright'", (self.match_id,))
        self.assertEqual(decision["reviewer"], "alice")
        self.assertEqual(decision["action"], "approve_sl")
        suggestions = self.review.detail(self.match_id)["suggestions"]
        self.assertEqual(suggestions[0]["status"], "accepted")
        self.assertEqual(suggestions[0]["model"], "test-model@1")

    def test_rejecting_suggestion_leaves_no_decision(self):
        row = [r for r in self.detail["comparison"]["copyrights"] if r["kind"] == "variant"][0]
        sid = self.review.add_suggestion(self.match_id, "copyright", row["key"], row["sl_text"], confidence=0.4,
                                         model="test-model@1")["id"]
        out = self.review.decide_suggestion(sid, "reject", "alice")
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(self.db.query("SELECT * FROM decisions WHERE match_id=?", (self.match_id,)), [])

    def test_suggestion_acceptance_is_validated(self):
        sid = self.review.add_suggestion(self.match_id, "license_text", "bogus-hash", "invented text",
                                         confidence=0.9, model="test-model@1")["id"]
        with self.assertRaises(ValueError):
            self.review.decide_suggestion(sid, "accept", "alice")
        self.assertEqual(self.review.detail(self.match_id)["suggestions"][0]["status"], "open")


if __name__ == "__main__":
    unittest.main()
