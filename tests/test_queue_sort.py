"""Queue ordering: the rows that need a reviewer come first, and paging stays stable."""
from __future__ import annotations

import unittest

from clreview.db import DB
from clreview.server import FIXTURES, seed_demo
from clreview.service import Service

from helpers import make_review


class QueueSortTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.addCleanup(self.db.close)
        self.mid = seed_demo(self.db)["mapping_id"]
        self.review, _ = make_review(Service(self.db, FIXTURES))

    def _items(self, **kw):
        return self.review.list_components(self.mid, **kw)["items"]

    def test_discrepancy_sort_puts_the_worst_rows_first(self):
        counts = [i["discrepancy_count"] for i in self._items(sort="discrepancies")]
        self.assertGreater(max(counts), 0, "fixture should contain a discrepant component")
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_confidence_sort_puts_the_least_certain_rows_first(self):
        conf = [i["confidence"] for i in self._items(sort="confidence")]
        self.assertEqual(conf, sorted(conf))

    def test_default_sort_is_by_name(self):
        names = [(i["pl_name"] or "").lower() for i in self._items()]
        self.assertEqual(names, sorted(names))

    def test_unknown_sort_falls_back_to_name(self):
        self.assertEqual([i["id"] for i in self._items(sort="nonsense")],
                         [i["id"] for i in self._items()])


if __name__ == "__main__":
    unittest.main()
