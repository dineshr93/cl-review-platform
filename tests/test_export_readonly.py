"""read_only kill switch (no vendor writes) and per-mapping CSV export of the review queue."""
from __future__ import annotations

import csv
import io
import json
import threading
import unittest
import urllib.error
import urllib.request

from clreview.api import serve
from clreview.db import DB
from clreview.server import FIXTURES, seed_demo


class Server:
    def __init__(self, db, read_only=False):
        self.httpd = serve(db, port=0, fixtures_dir=FIXTURES, read_only=read_only)
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

    def get_raw(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.mapping_id = seed_demo(self.db)["mapping_id"]
        self.server = Server(self.db, read_only=True)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_vendor_write_routes_are_refused_with_409(self):
        for path, body in (("/api/sync/jobs", {"mapping_id": self.mapping_id}),
                           ("/api/deploy", {"pl_connection_id": 1, "sl_connection_id": 2,
                                            "project_id": "p", "version_id": "v"}),
                           ("/api/sync/jobs/1/retry", {})):
            status, err = self.server.call("POST", path, body, reviewer="alice")
            self.assertEqual(status, 409, path)
            self.assertIn("read-only", err["error"])
            self.assertTrue(err["read_only"])

    def test_reads_refresh_and_local_review_still_work(self):
        self.assertEqual(self.server.call("GET", "/api/mappings")[0], 200)
        self.assertEqual(self.server.call("POST", f"/api/mappings/{self.mapping_id}/refresh",
                                          reviewer="alice")[0], 200)
        self.assertEqual(self.server.call("POST", "/api/sync/preview",
                                          {"mapping_id": self.mapping_id})[0], 200)

    def test_health_reports_the_mode(self):
        _status, body = self.server.call("GET", "/api/health")
        self.assertTrue(body["read_only"])

    def test_writes_are_not_blocked_without_the_flag(self):
        self.server.close()
        open_server = Server(self.db, read_only=False)
        self.addCleanup(open_server.close)
        status, err = open_server.call("POST", "/api/sync/jobs/1/retry", {}, reviewer="alice")
        self.assertNotEqual(status, 409)
        self.assertNotEqual(err.get("read_only"), True)


class ExportCsvTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.mapping_id = seed_demo(self.db)["mapping_id"]
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_export_returns_one_csv_row_per_component(self):
        status, _page = self.server.call("GET", f"/api/mappings/{self.mapping_id}/components?limit=200")
        self.assertEqual(status, 200)
        queue, total = _page["items"], _page["total"]
        status, ctype, text = self.server.get_raw(f"/api/mappings/{self.mapping_id}/export.csv")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/csv"), ctype)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), total)
        self.assertEqual({r["pl_name"] for r in rows}, {c["pl_name"] for c in queue})
        for column in ("pl_version", "match_status", "review_status", "licenses"):
            self.assertIn(column, rows[0])


if __name__ == "__main__":
    unittest.main()
