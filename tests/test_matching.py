"""Matching hierarchy tests: no weak fuzzy match may auto-merge components."""
from __future__ import annotations

import unittest
from collections import Counter
from typing import Any

from clreview.db import DB
from clreview.domain import ComponentData, CopyrightEntry, LicenseEntry, LicenseTextCandidate, sha256
from clreview.matching import match_components
from clreview.service import IN_CHUNK, Service


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


STAMP = "2026-01-01T00:00:00+00:00"


def inventory(n: int) -> list[ComponentData]:
    """n components, identical on both sides: the shape that made matching superlinear."""
    out = []
    for i in range(n):
        name = "lib%d" % i
        out.append(ComponentData(
            external_id="%s|1.0" % name, name=name, version="1.0", purl="pkg:gen/%s@1.0" % name,
            copyrights=[CopyrightEntry(text="Copyright (C) 2020 Owner %d" % i, origin="kb")],
            licenses=[LicenseEntry(name="MIT", spdx="MIT")],
            license_texts=[LicenseTextCandidate(kind="source_file",
                                                text="Permission is hereby granted, free of charge, item %d" % i,
                                                provenance="src", content_hash=sha256("text%d" % i))],
        ))
    return out


class MatchingScaleTests(unittest.TestCase):
    """_run_matching must read in a bounded number of statements, not one per component."""

    N = 1600

    def setUp(self):
        self.db = DB(":memory:")
        self.svc = Service(self.db)
        for role, tool in (("pl", "mock-pl"), ("sl", "mock-sl")):
            self.db.execute("INSERT INTO connections(role,tool,name,created_at) VALUES(?,?,?,?)",
                            (role, tool, tool, STAMP))
        self.mid = self.db.execute(
            "INSERT INTO mappings(name,pl_connection_id,sl_connection_id,pl_project,pl_version,sl_scan,created_at)"
            " VALUES(?,?,?,?,?,?,?)", ("scale", 1, 2, "proj", "1.0", "scan", STAMP)).lastrowid
        self.store()

    def tearDown(self):
        self.db.close()

    def store(self) -> None:
        inv = inventory(self.N)
        self.svc._store_snapshot(self.mid, "pl", "mock-pl", inv)
        self.svc._store_snapshot(self.mid, "sl", "mock-sl", inv)

    def counted_matching(self) -> tuple[Counter, dict[str, Any]]:
        counts: Counter = Counter()
        original = {"query": self.db.query, "one": self.db.one, "execute": self.db.execute}

        def shape(sql: str) -> str:
            words = " ".join(sql.split()).split(" ")
            head = words[0].upper()
            if head == "SELECT":
                return "SELECT " + sql.split("FROM")[1].split()[0]
            if head == "UPDATE":
                return "UPDATE " + words[1]
            return head

        def wrap(fn):
            def inner(sql, params=()):
                counts["TOTAL"] += 1
                counts[shape(sql)] += 1
                if "component_id=?" in sql:
                    counts["PER_COMPONENT"] += 1
                return fn(sql, params)
            return inner

        for name, fn in original.items():
            setattr(self.db, name, wrap(fn))
        try:
            result = self.svc._run_matching(self.mid)
        finally:
            for name, fn in original.items():
                setattr(self.db, name, fn)
        return counts, result

    def test_matching_reads_in_bounded_statements(self):
        counts, result = self.counted_matching()
        self.assertEqual(result["proposed"], self.N)
        self.assertEqual(result["new_matches"], self.N)
        self.assertEqual(counts["PER_COMPONENT"], 0, "observations must be batched, never read per component")
        self.assertLess(counts["TOTAL"], 6000, dict(counts))
        ceiling = 2 * (self.N // IN_CHUNK + 1)
        for table in ("copyrights", "licenses", "license_texts"):
            self.assertLessEqual(counts["SELECT " + table], ceiling, dict(counts))

    def test_a_refresh_that_changed_nothing_recomputes_nothing(self):
        self.counted_matching()
        self.store()
        counts, result = self.counted_matching()
        self.assertEqual(result["new_matches"], 0)
        self.assertEqual(counts["INSERT"], 0, dict(counts))
        self.assertEqual(counts["UPDATE matches"], 0, dict(counts))
        self.assertLess(counts["TOTAL"], 200, dict(counts))


if __name__ == "__main__":
    unittest.main()
