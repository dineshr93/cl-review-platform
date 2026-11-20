"""License map proposals: SL names matched to PL licenses through the SPDX list."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from clreview.domain import license_key, license_relation

from helpers import make_service, pl_component, tmpdir

SPDX = {"licenses": [
    {"licenseId": "GPL-2.0-only", "name": "GNU General Public License v2.0 only"},
    {"licenseId": "Apache-2.0", "name": "Apache License 2.0"},
    {"licenseId": "BSD-3-Clause", "name": 'BSD 3-Clause "New" or "Revised" License'},
    {"licenseId": "MIT", "name": "MIT License"},
]}


class LicenseKey(unittest.TestCase):
    def test_numbers_are_identity_and_filler_words_drop(self):
        self.assertEqual(license_key("Apache License, Version 2.0"), license_key("Apache-2.0"))
        self.assertIsNone(license_relation(license_key("GPL-3.0"), license_key("GPL-2.0")))
        self.assertEqual(license_relation(license_key('BSD 3-clause "New" or "Revised" License'),
                                          license_key("BSD-3-Clause")), "subset")
        self.assertNotEqual(license_key("GPL-2.0+"), license_key("GPL-2.0"))


class Suggestions(unittest.TestCase):
    def setUp(self):
        d = tmpdir()
        pl = [pl_component(name="a", version="1.0", licenses=[{"name": "MIT", "spdx": "MIT"}])]
        sl = [{"external_id": "a-1.0", "name": "a", "version": "1.0",
               "licenses": [{"name": n} for n in ("MIT", "GPL-2.0-only", "BSD-3-Clause", "Apache 2",
                                                  "No_license_found", "WeirdLicense-7")]}]
        self.svc, self.db, self.mid = make_service(d, pl, sl)
        spdx = os.path.join(d, "spdx.json")
        with open(spdx, "w", encoding="utf-8") as fh:
            json.dump(SPDX, fh)
        patcher = mock.patch.dict(os.environ, {"CL_SPDX_URL": spdx})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_three_sources_propose_and_only_confident_ones_are_recommended(self):
        r = self.svc.suggest_license_map()
        got = {p["short_name"]: p for p in r["items"]}
        self.assertEqual(set(got), {"GPL-2.0-only", "BSD-3-Clause", "Apache 2", "WeirdLicense-7"})
        gpl = got["GPL-2.0-only"]  # the seed maps it to a name the PL catalogue does not have
        self.assertEqual((gpl["action"], gpl["bd_license"], gpl["method"], gpl["recommended"]),
                         ("update", "GNU General Public License v2.0 only", "spdx_id", True))
        apache = got["Apache 2"]  # found in SPDX by the name rule, then in PL by SPDX id
        self.assertEqual((apache["action"], apache["spdx"], apache["bd_license"]), ("add", "Apache-2.0", "Apache License 2.0"))
        bsd = got["BSD-3-Clause"]  # the PL name is SPDX's full name, word for word
        self.assertEqual((bsd["method"], bsd["recommended"]), ("name_same", True))
        self.assertEqual((got["WeirdLicense-7"]["action"], got["WeirdLicense-7"]["bd_license"]), ("unresolved", None))
        self.assertEqual((r["skipped"]["unchanged"], r["skipped"]["not_a_license"]), (1, 1))

    def test_apply_updates_the_existing_row_and_a_reviewer_entry_is_never_proposed_over(self):
        before = len(self.svc.license_map_rows())
        self.svc.apply_license_map([{"short_name": "GPL-2.0-only", "spdx": "GPL-2.0-only",
                                     "bd_license": "GNU General Public License v2.0 only"}], "alice")
        self.svc.upsert_license_map("bsd-3-clause", None, "BSD-3-Clause", "alice")
        rows = self.svc.license_map_rows()
        self.assertEqual(len(rows), before)  # both writes updated the seed rows in place
        self.assertEqual(self.svc.license_map_lookup()["GPL-2.0-ONLY"]["source"], "suggested")
        got = {p["short_name"] for p in self.svc.suggest_license_map()["items"]}
        self.assertNotIn("GPL-2.0-only", got)
        self.assertNotIn("BSD-3-Clause", got)

    def test_without_spdx_a_word_subset_is_shown_but_not_preselected(self):
        with mock.patch.dict(os.environ, {"CL_SPDX_URL": os.path.join(tmpdir(), "missing.json")}):
            r = self.svc.suggest_license_map()
        self.assertTrue(any("SPDX" in w for w in r["warnings"]))
        bsd = {p["short_name"]: p for p in r["items"]}["BSD-3-Clause"]
        self.assertEqual((bsd["method"], bsd["recommended"]), ("name_subset", False))


if __name__ == "__main__":
    unittest.main()
