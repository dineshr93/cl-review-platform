"""Bulk work, review-only approval, rules, and license text per Black Duck license."""
from __future__ import annotations

import csv
import io
import unittest

from clreview.sync import SyncService

from helpers import make_review, make_service, paths_for, pl_component, rewrite_fixtures, tmpdir


def two_license_pair():
    """One component with two PL licenses, each with only its generic KB text, and an SL upload
    whose license files the summary read does not include (they come from license_files)."""
    pl = [pl_component(
        name="dual", version="1.0",
        licenses=[{"name": "MIT"}, {"name": "BSD-3-Clause"}],
        license_texts=[{"kind": "generic_metadata", "text": "MIT generic KB text", "file_name": "MIT", "provenance": "kb"},
                       {"kind": "generic_metadata", "text": "BSD generic KB text", "file_name": "BSD-3-Clause",
                        "provenance": "kb"}])]
    sl = [{"external_id": "dual-1.0", "name": "dual", "version": "1.0", "metadata": {"upload_id": "u1"},
           "licenses": [{"name": "MIT"}, {"name": "BSD-3-Clause"}],
           "license_files": [{"file_path": "dual/LICENSE-MIT", "text": "MIT original text", "licenses": ["MIT"]},
                             {"file_path": "dual/COPYING", "text": "BSD original text", "licenses": ["BSD-3-Clause"]}]}]
    return pl, sl


class ReviewOnlyApproval(unittest.TestCase):
    def setUp(self):
        same = {"copyrights": [{"text": "Copyright 2020 Owner"}], "licenses": [{"name": "MIT"}]}
        pl = [pl_component(name="agree", version="1.0", **same), pl_component(name="fuzzy", version="2.0")]
        sl = [{"external_id": "agree-1.0", "name": "agree", "version": "1.0", **same},
              {"external_id": "fuzzy-2.1", "name": "fuzzy", "version": "2.1"}]
        self.svc, self.db, self.mid = make_service(tmpdir(), pl, sl)
        self.review, self.sync = make_review(self.svc)
        items = {i["pl_name"]: i for i in self.review.list_components(self.mid)["items"]}
        self.agree, self.fuzzy = items["agree"]["id"], items["fuzzy"]["id"]

    def test_a_component_that_agrees_is_approved_and_the_sync_only_marks_it_reviewed(self):
        out = self.review.set_review_state(self.agree, "approved_for_sync", "alice")
        self.assertTrue(out["review_only"])
        job = self.sync.create_job(self.mid, dry_run=False, confirm=True, finalize=True, actor="alice")
        self.assertEqual({(i["field"], i["status"]) for i in job["items"] if i["field"] == "review_status"},
                         {("review_status", "succeeded")})
        self.assertFalse([i for i in job["items"] if i["field"] in ("copyright", "license_name", "license_text")])
        pl = self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"])
        self.assertEqual(pl.read_component("proj|1.0|agree")["reviewStatus"], "REVIEWED")

    def test_a_suggested_match_must_be_confirmed_first(self):
        with self.assertRaisesRegex(ValueError, "confirm the suggested match"):
            self.review.set_review_state(self.fuzzy, "approved_for_sync", "alice")

    def test_bulk_approves_what_fits_and_reports_the_rest(self):
        r = self.review.bulk(self.mid, "approve", [self.agree, self.fuzzy], "alice")
        self.assertEqual((r["done"], r["review_only"]), ([self.agree], 1))
        self.assertEqual([f["id"] for f in r["failed"]], [self.fuzzy])
        self.assertIn("confirm", r["failed"][0]["error"])

        r = self.review.bulk(self.mid, "confirm", [self.fuzzy], "alice")
        self.assertEqual(r["done"], [self.fuzzy])
        self.assertEqual(self.review.match(self.fuzzy)["status"], "confirmed")

    def test_a_selection_is_explicit_ids_of_this_mapping_or_the_filtered_set(self):
        with self.assertRaisesRegex(ValueError, "do not belong"):
            self.review.resolve_selection(self.mid, [self.agree, 99999])
        self.assertEqual(self.review.resolve_selection(self.mid, filters={"status": "suggested"}), [self.fuzzy])

    def test_export_of_a_selection(self):
        rows = list(csv.DictReader(io.StringIO(self.review.export_csv(self.mid, {self.agree}))))
        self.assertEqual([r["pl_name"] for r in rows], ["agree"])


class BulkDecisionsAndRules(unittest.TestCase):
    def setUp(self):
        pl = [pl_component(name="lib", version="1.0", copyrights=[
            {"text": "Copyright  2020   Owner"}, {"text": "Copyright 2019 Someone"}, {"text": "(c) Old Holder"}])]
        sl = [{"external_id": "lib-1.0", "name": "lib", "version": "1.0", "copyrights": [
            {"text": "copyright 2020 owner"}, {"text": "Copyright 2018 Someone"}, {"text": "(c) New Holder Inc"}]}]
        self.svc, self.db, self.mid = make_service(tmpdir(), pl, sl)
        self.review, _ = make_review(self.svc)
        self.match = self.review.list_components(self.mid)["items"][0]["id"]

    def kinds(self):
        return {r["key"]: r for r in self.review.detail(self.match)["comparison"]["copyrights"]}

    def test_the_formatting_rule_decides_only_notices_identical_after_normalizing(self):
        rule = next(r for r in self.review.rules(self.mid) if r["name"] == "copyright_formatting")
        self.assertEqual(rule["rows"], 1)   # the 2019/2018 pair is a near-variant: left for a person
        self.assertEqual(self.review.apply_rule(self.mid, "copyright_formatting", "alice")["decided"], 1)
        decided = [r for r in self.kinds().values() if r["decision"]]
        self.assertEqual([r["sl_text"] for r in decided], ["copyright 2020 owner"])
        self.assertIn("rule:", decided[0]["decision"]["note"])
        self.assertEqual(self.review.rules(self.mid)[0]["rows"], 0)

    def test_many_rows_in_one_call_with_refusals_reported(self):
        rows = self.kinds()
        keys = [k for k, r in rows.items() if r["kind"] != "matched"]
        out = self.review.decide_many(self.match, "copyright", keys + ["not-a-key"], "approve_sl", "alice")
        self.assertEqual(sorted(out["done"]), sorted(k for k in keys if rows[k]["sl_text"]))
        self.assertTrue(any(f["entry_key"] == "not-a-key" for f in out["failed"]))
        with self.assertRaisesRegex(ValueError, "bulk decisions"):
            self.review.decide_many(self.match, "copyright", keys, "correct", "alice")


class LicenseTextPerLicense(unittest.TestCase):
    def setUp(self):
        pl, sl = two_license_pair()
        self.svc, self.db, self.mid = make_service(tmpdir(), pl, sl)
        self.svc.test_connection(self.svc.mapping(self.mid)["pl_connection_id"], "t")
        self.review, _ = make_review(self.svc)
        self.sync = SyncService(self.svc)
        self.match = self.review.list_components(self.mid)["items"][0]["id"]

    def rows(self):
        return {t["license"]: t for t in self.review.detail(self.match)["comparison"]["license_texts"]}

    def test_each_license_is_its_own_row_and_loaded_files_are_assigned_to_it(self):
        self.assertEqual(set(self.rows()), {"MIT", "BSD-3-Clause"})
        self.assertIsNone(self.rows()["MIT"]["preferred"])          # summary evidence has no file text
        self.assertEqual(self.review.load_sl_license_files(self.match, "alice")["files"], 2)
        rows = self.rows()
        self.assertEqual(rows["MIT"]["preferred"]["file_path"], "dual/LICENSE-MIT")
        self.assertEqual(rows["BSD-3-Clause"]["preferred"]["file_path"], "dual/COPYING")
        self.assertEqual(rows["MIT"]["pl_current"]["text"], "MIT generic KB text")

    def test_the_sync_writes_one_license_text_and_leaves_the_other(self):
        self.review.load_sl_license_files(self.match, "alice")
        mit, bsd = self.rows()["MIT"], self.rows()["BSD-3-Clause"]
        self.review.decide(self.match, "license_text", mit["preferred"]["content_hash"] + mit["suffix"], "approve_sl",
                           reviewer="alice")
        self.review.decide(self.match, "license_text", bsd["discrepancy_key"], "accept_pl", reviewer="alice")
        self.review.set_review_state(self.match, "approved_for_sync", "alice")
        job = self.sync.create_job(self.mid, dry_run=False, confirm=True, actor="alice")
        texts = [i for i in job["items"] if i["field"] == "license_text"]
        self.assertEqual([i["status"] for i in texts], ["succeeded"])
        pl = self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"])
        stored = {t["file_name"]: t["text"] for t in pl.read_component("proj|1.0|dual")["license_texts"]}
        self.assertEqual(stored, {"MIT": "MIT original text", "BSD-3-Clause": "BSD generic KB text"})

    def test_reviewer_supplied_text_needs_a_source_and_settles_the_row(self):
        key = "reviewer_supplied" + self.rows()["MIT"]["suffix"]
        with self.assertRaisesRegex(ValueError, "note is required"):
            self.review.decide(self.match, "license_text", key, "correct", approved_value="pasted", reviewer="alice")
        self.review.decide(self.match, "license_text", key, "correct", approved_value="pasted MIT text",
                           note="reviewer-supplied text; source: FOSSology file view", reviewer="alice")
        self.assertEqual(self.rows()["MIT"]["decision"]["approved_value"], "pasted MIT text")
        detail = self.review.detail(self.match)
        self.assertEqual(detail["review_status"], "in_progress")   # BSD still open; MIT is settled

    def test_loaded_files_stop_counting_once_the_sl_observations_change(self):
        self.review.load_sl_license_files(self.match, "alice")
        pl, sl = two_license_pair()
        sl[0]["copyrights"] = [{"text": "Copyright 2024 New Owner"}]
        rewrite_fixtures(paths_for(self.svc, self.mid), sl_components=sl)
        self.svc.refresh(self.mid, "alice")
        self.assertIsNone(self.rows()["MIT"]["preferred"])
        self.assertEqual(self.review.detail(self.match)["comparison"]["loaded_files"], [])


if __name__ == "__main__":
    unittest.main()
