"""Matching hierarchy tests: no weak fuzzy match may auto-merge components."""
from __future__ import annotations

import unittest

from clreview.domain import ComponentData
from clreview.matching import match_components


def comp(key, name, version=None, purl=None, ids=None):
    return ComponentData(external_id=key, name=name, version=version, purl=purl,
                         metadata={"external_ids": ids or []}, raw={"external_ids": ids or []})


class MatchingTests(unittest.TestCase):
    def test_identical_name_and_version(self):
        out = match_components([comp("p1", "zlib", "1.2.11")], [comp("s1", "zlib", "1.2.11")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].method, "name_version")
        self.assertEqual(out[0].confidence, 0.95)
        self.assertEqual(out[0].status, "auto")

    def test_different_names_same_purl(self):
        out = match_components(
            [comp("p1", "lodash.js", "4.17.21", purl="pkg:npm/lodash@4.17.21")],
            [comp("s1", "lodash", "4.17.21", purl="pkg:NPM/lodash@4.17.21")],
        )
        self.assertEqual(out[0].method, "purl")
        self.assertEqual(out[0].confidence, 0.99)

    def test_version_missing_on_one_side(self):
        out = match_components([comp("p1", "libfoo", "1.0")], [comp("s1", "libfoo")])
        self.assertEqual(out[0].method, "name_only")
        self.assertEqual(out[0].confidence, 0.72)
        self.assertIn("version differs", " ".join(out[0].mismatch_reasons))

    def test_similar_names_do_not_cross_match(self):
        pl = [comp("p1", "zlib", "1.2.11"), comp("p2", "zlib-ng", "2.1.2")]
        sl = [comp("s1", "zlib", "1.2.11"), comp("s2", "zlib-ng", "2.1.2")]
        out = match_components(pl, sl)
        pairs = {(r.pl_key, r.sl_key) for r in out}
        self.assertIn(("p1", "s1"), pairs)
        self.assertIn(("p2", "s2"), pairs)
        self.assertNotIn(("p1", "s2"), pairs)
        self.assertNotIn(("p2", "s1"), pairs)

    def test_fuzzy_is_only_a_suggestion(self):
        out = match_components([comp("p1", "libpng16", "1.6.37")], [comp("s1", "libpng", "1.6.37")])
        self.assertEqual(out[0].method, "fuzzy")
        self.assertEqual(out[0].status, "suggested")
        self.assertLess(out[0].confidence, 0.7)
        self.assertIn("reviewer confirmation", " ".join(out[0].rationale))

    def test_unmatched_both_directions(self):
        out = match_components([comp("p1", "left-pad", "1.0")], [comp("s1", "extra-lib", "2.0")])
        statuses = sorted(r.status for r in out)
        self.assertEqual(statuses, ["unmatched", "unmatched"])
        self.assertTrue(any(r.pl_key == "p1" and r.sl_key == "" for r in out))
        self.assertTrue(any(r.sl_key == "s1" and r.pl_key == "" for r in out))

    def test_shared_strong_identifier(self):
        out = match_components([comp("p1", "alpha", "1.0", ids=["bd-123"])],
                               [comp("s1", "alpha-core", "1.0.0", ids=["bd-123"])])
        self.assertEqual(out[0].method, "strong_id")
        self.assertEqual(out[0].confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
