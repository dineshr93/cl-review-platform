"""Comparison tests: copyright variants, license distinctions, text provenance."""
from __future__ import annotations

import unittest

from clreview.comparison import compare_copyrights, compare_license_texts, compare_licenses
from clreview.domain import CopyrightEntry, LicenseEntry, LicenseTextCandidate


def ce(text, origin="test"):
    return CopyrightEntry(text=text, origin=origin)


def le(name, spdx=None, paths=None, ltype="declared"):
    return LicenseEntry(name=name, spdx=spdx, evidence_paths=list(paths or []), license_type=ltype, source="test")


def te(kind, text, **kw):
    return LicenseTextCandidate(kind=kind, text=text, provenance=kw.get("provenance", "test"),
                                file_name=kw.get("file_name"), file_path=kw.get("file_path"),
                                is_license_file=kw.get("is_license_file", False),
                                content_hash=kw.get("content_hash"))


class CopyrightTests(unittest.TestCase):
    def test_multiple_notices_match(self):
        pl = [ce("Copyright (C) 2018 Cosmin Truta"), ce("Copyright (C) 1998 Glenn Randers-Pehrson")]
        sl = [ce("Copyright (C) 1998 Glenn Randers-Pehrson"), ce("Copyright (C) 2018 Cosmin Truta")]
        rows = compare_copyrights(pl, sl)
        self.assertTrue(all(r.kind == "matched" for r in rows))
        self.assertEqual(len(rows), 2)

    def test_formatting_variant_is_surfaced(self):
        rows = compare_copyrights([ce("Copyright (C) 1995-2017 Jean-loup Gailly")],
                                  [ce("Copyright  1995-2017  Jean-loup Gailly")])
        self.assertEqual(rows[0].kind, "variant")
        self.assertNotEqual(rows[0].pl_text, rows[0].sl_text)

    def test_duplicate_within_one_side(self):
        rows = compare_copyrights([ce("Copyright 2020 A"), ce("Copyright  2020   A")],
                                  [ce("Copyright 2020 A")])
        kinds = sorted(r.kind for r in rows)
        self.assertIn("duplicate", kinds)
        self.assertIn("matched", kinds)

    def test_one_sided_notices(self):
        rows = compare_copyrights([ce("Copyright 2020 PL only")], [ce("Copyright 2021 SL only")])
        kinds = sorted(r.kind for r in rows)
        self.assertEqual(kinds, ["pl_only", "sl_only"])


class LicenseTests(unittest.TestCase):
    def test_similar_names_are_not_equivalent(self):
        rows = compare_licenses([le("MIT")], [le("MIT-0")])
        kinds = sorted(r.kind for r in rows)
        self.assertEqual(kinds, ["pl_only", "sl_only"])

    def test_spdx_difference_is_flagged(self):
        rows = compare_licenses([le("GPL-2.0", "GPL-2.0")], [le("GPL-2.0", "GPL-2.0-only")])
        self.assertEqual(rows[0].kind, "spdx_diff")

    def test_alternative_licenses_keep_their_type(self):
        rows = compare_licenses([le("MIT", "MIT")], [le("MIT", "MIT", ltype="alternative")])
        self.assertEqual(rows[0].license_type, "alternative")

    def test_evidence_paths_are_kept(self):
        rows = compare_licenses([], [le("MIT", "MIT", paths=["pkg/LICENSE"])])
        self.assertEqual(rows[0].evidence_paths, ["pkg/LICENSE"])


class LicenseTextTests(unittest.TestCase):
    def test_generic_pl_text_vs_source_file(self):
        cmp = compare_license_texts(
            [te("generic_metadata", "canonical text", provenance="kb")],
            [te("generic_metadata", "canonical text", provenance="fossology"),
             te("source_file", "original license text", file_name="LICENSE", file_path="pkg/LICENSE", is_license_file=True)],
        )
        self.assertEqual(cmp.recommendation, "replace_with_source")
        self.assertEqual(cmp.preferred.file_path, "pkg/LICENSE")
        self.assertTrue(any("generic license metadata" in r for r in cmp.reasons))

    def test_prefers_license_over_notice(self):
        cmp = compare_license_texts([], [
            te("source_file", "notice text", file_name="NOTICE", file_path="pkg/NOTICE", is_license_file=True),
            te("source_file", "license text", file_name="LICENSE.txt", file_path="pkg/LICENSE.txt", is_license_file=True),
            te("source_file", "copying text", file_name="COPYING", file_path="pkg/COPYING", is_license_file=True),
        ])
        self.assertEqual(cmp.preferred.file_name, "LICENSE.txt")
        self.assertTrue(any("candidate license files" in r for r in cmp.reasons))

    def test_no_source_text_is_flagged(self):
        cmp = compare_license_texts([te("generic_metadata", "canonical")],
                                    [te("generic_metadata", "canonical")])
        self.assertEqual(cmp.recommendation, "no_source_text")
        self.assertIsNone(cmp.preferred)

    def test_unknown_provenance_is_not_preferred(self):
        cmp = compare_license_texts([], [te("unknown", "mystery text", file_name="LICENSE", is_license_file=True)])
        self.assertEqual(cmp.recommendation, "no_source_text")
        self.assertTrue(any("provenance is unknown" in r for r in cmp.reasons))

    def test_already_matching_source_text(self):
        cmp = compare_license_texts(
            [te("source_file", "same", file_name="LICENSE", is_license_file=True)],
            [te("source_file", "same", file_name="LICENSE", file_path="pkg/LICENSE", is_license_file=True)],
        )
        self.assertEqual(cmp.recommendation, "already_matches")


if __name__ == "__main__":
    unittest.main()
