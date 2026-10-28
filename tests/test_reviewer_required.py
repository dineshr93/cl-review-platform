"""A decision or write is never recorded without a reviewer name (open identity, attributed)."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from clreview.api import _safe_reviewer, serve
from clreview.db import DB
from clreview.server import FIXTURES, seed_demo


class Server:
    def __init__(self, db):
        self.httpd = serve(db, port=0, fixtures_dir=FIXTURES)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, reviewer=None):
        headers = {"Content-Type": "application/json"}
        if reviewer is not None:
            headers["X-Reviewer"] = reviewer
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class ReviewerRequiredTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.mapping_id = seed_demo(self.db)["mapping_id"]
        self.server = Server(self.db)
        self.refresh = f"/api/mappings/{self.mapping_id}/refresh"

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_write_without_a_reviewer_header_is_refused(self):
        status, body = self.server.call("POST", self.refresh)
        self.assertEqual(status, 400)
        self.assertTrue(body.get("reviewer_required"))
        self.assertIn("Reviewer name required", body["error"])

    def test_blank_reviewer_is_refused(self):
        for raw in ("", "   ", "\t"):
            status, body = self.server.call("POST", self.refresh, reviewer=raw)
            self.assertEqual(status, 400, raw)
            self.assertIn("top right", body["error"])

    def test_named_reviewer_is_allowed(self):
        status, _ = self.server.call("POST", self.refresh, reviewer="alice")
        self.assertEqual(status, 200)

    def test_reads_and_read_shaped_posts_still_work_unnamed(self):
        self.assertEqual(self.server.call("GET", "/api/mappings")[0], 200)
        self.assertEqual(self.server.call("POST", "/api/sync/preview", {"mapping_id": self.mapping_id})[0], 200)

    def test_sanitizer_keeps_real_names_and_drops_dangerous_ones(self):
        self.assertEqual(_safe_reviewer(" Zoë Chen "), "Zoë Chen")
        self.assertEqual(_safe_reviewer("dinesh.r@corp"), "dinesh.r@corp")
        self.assertEqual(_safe_reviewer('al<ice>"&\\\x00'), "alice")
        self.assertEqual(_safe_reviewer("   "), "")
        self.assertEqual(len(_safe_reviewer("x" * 500)), 80)


if __name__ == "__main__":
    unittest.main()
