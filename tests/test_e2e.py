"""End-to-end workflow over the HTTP API: retrieve -> match -> review -> dry-run -> sync -> verify."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from clreview.api import serve
from clreview.db import DB
from clreview.server import FIXTURES, seed_demo
from clreview.service import Service


class Server:
    def __init__(self, db, token=None):
        self.httpd = serve(db, port=0, fixtures_dir=FIXTURES, auth_token=token)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, reviewer="alice", token=None):
        headers = {"Content-Type": "application/json", "X-Reviewer": reviewer}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        info = seed_demo(self.db)
        self.mapping_id = info["mapping_id"]
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_full_workflow(self):
        status, mappings = self.server.call("GET", "/api/mappings")
        self.assertEqual(status, 200)
        self.assertEqual(len(mappings["items"]), 1)

        _, queue = self.server.call("GET", f"/api/mappings/{self.mapping_id}/components?q=zlib")
        zlib = [i for i in queue["items"] if i["pl_name"] == "zlib" and i["status"] != "unmatched"][0]
        mid = zlib["id"]

        _, detail = self.server.call("GET", f"/api/components/{mid}")
        self.assertEqual(detail["review_status"], "review_required")
        variant = [r for r in detail["comparison"]["copyrights"] if r["kind"] in ("variant", "sl_only")][0]
        pref = detail["comparison"]["license_text"]["preferred"]
        self.assertEqual(detail["comparison"]["license_text"]["recommendation"], "replace_with_source")

        _, d1 = self.server.call("POST", f"/api/components/{mid}/decisions",
                                 {"field": "copyright", "entry_key": variant["key"], "action": "approve_sl"})
        self.assertEqual(d1["review_status"], "in_progress")
        _, d2 = self.server.call("POST", f"/api/components/{mid}/decisions",
                                 {"field": "license_text", "entry_key": pref["content_hash"], "action": "approve_sl"})
        self.assertEqual(d2["review_status"], "reviewed")

        _, approved = self.server.call("POST", f"/api/components/{mid}/review", {"state": "approved_for_sync"})
        self.assertEqual(approved["review_status"], "approved_for_sync")

        _, preview = self.server.call("POST", "/api/sync/preview", {"mapping_id": self.mapping_id})
        self.assertTrue(preview["components"])
        self.assertTrue(all(i["status"] == "ready" for c in preview["components"] for i in c["items"]))

        _, dry = self.server.call("POST", "/api/sync/jobs", {"mapping_id": self.mapping_id, "dry_run": True})
        self.assertEqual(dry["status"], "dry_run")
        self.assertEqual(dry["summary"], {})

        _, live = self.server.call("POST", "/api/sync/jobs", {"mapping_id": self.mapping_id, "dry_run": False, "confirm": True})
        self.assertEqual(live["status"], "completed")
        self.assertEqual(live["summary"], {"succeeded": 2})

        _, job = self.server.call("GET", f"/api/sync/jobs/{live['id']}")
        self.assertTrue(all(i["status"] == "succeeded" for i in job["items"]))
        self.assertTrue(all(i["verified_value"] for i in job["items"]))

        svc = self.server.httpd.api.service  # same connector instance the server wrote through
        pl_key = "demo-app|1.0.0|zlib"
        pl = svc.connector_for(svc.mapping(self.mapping_id)["pl_connection_id"]).read_component(pl_key)
        self.assertIn("Copyright  1995-2017  Jean-loup Gailly and Mark Adler", pl["copyrights"])
        self.assertEqual(pl["license_texts"][0]["file_name"], "LICENSE")

        _, audit = self.server.call("GET", f"/api/audit?mapping_id={self.mapping_id}")
        actions = {a["action"] for a in audit["items"]}
        self.assertTrue({"decision.approve_sl", "review.approved_for_sync", "sync.item"} <= actions)

        _, rerun = self.server.call("POST", "/api/sync/jobs", {"mapping_id": self.mapping_id, "dry_run": False, "confirm": True})
        self.assertEqual(rerun["summary"], {"skipped_already_applied": 2})

    def test_live_sync_requires_confirm(self):
        status, body = self.server.call("POST", "/api/sync/jobs", {"mapping_id": self.mapping_id, "dry_run": False})
        self.assertEqual(status, 400)
        self.assertIn("confirm", body["error"])

    def test_static_index_is_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.port}/", timeout=5) as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("Copyright &amp; License Review", html)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        seed_demo(self.db)
        self.server = Server(self.db, token="test-token")

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_token_required_and_reviewer_recorded(self):
        status, body = self.server.call("GET", "/api/health")  # liveness stays public for probes
        self.assertEqual(status, 200)
        self.assertEqual(body["db"], "ok")
        status, _ = self.server.call("GET", "/api/connections")
        self.assertEqual(status, 401)
        status, _ = self.server.call("GET", "/api/health", token="test-token")
        self.assertEqual(status, 200)
        status, body = self.server.call("GET", "/api/version", token="test-token")
        self.assertEqual(status, 200)
        self.assertIn("version", body)
        status, body = self.server.call("POST", "/api/connections/1/test", {}, reviewer="bob", token="test-token")
        self.assertEqual(status, 200)
        with self.db.conn:
            rows = self.db.query("SELECT actor FROM audit WHERE action='connection.tested'")
        self.assertEqual(rows[-1]["actor"], "bob")


class BadInputTests(unittest.TestCase):
    """Malformed input is a client error (400), never a 500 or a misleading 404."""

    def setUp(self):
        self.db = DB(":memory:")
        self.mapping_id = seed_demo(self.db)["mapping_id"]
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_null_and_missing_ids_are_400(self):
        for path, body in (("/api/sync/preview", {"mapping_id": None}),
                           ("/api/sync/preview", {}),
                           ("/api/sync/jobs", {"mapping_id": None, "dry_run": True}),
                           ("/api/deploy", {"pl_connection_id": None}),
                           ("/api/mappings", {"name": "x"})):
            status, payload = self.server.call("POST", path, body)
            self.assertEqual(status, 400, (path, body))
            self.assertIn("required", payload["error"])

    def test_non_numeric_id_is_400_with_a_clean_message(self):
        status, payload = self.server.call("POST", "/api/sync/jobs", {"mapping_id": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "mapping_id must be an integer")


if __name__ == "__main__":
    unittest.main()
