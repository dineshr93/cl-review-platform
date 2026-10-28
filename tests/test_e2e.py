"""End-to-end workflow over the HTTP API: retrieve -> match -> review -> dry-run -> sync -> verify."""
from __future__ import annotations

import http.client
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from clreview import api as api_module
from clreview.api import serve
from clreview.connectors.base import ConnectorError
from clreview.connectors.http import UnsupportedOperation
from clreview.db import DB
from clreview.server import FIXTURES, seed_demo

from helpers import tmpdir


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

    def raw(self, method, path, body=None, token=None, reviewer="alice", extra=None):
        """Send a path or body verbatim - urllib normalises '..' and rewrites headers.

        A body that is an iterator is streamed (http.client then adds ``Transfer-Encoding:
        chunked``), and the server may answer by closing the connection while the client is still
        writing. The socket is closed either way; the exception is left to the caller, which knows
        whether a hung-up connection is an expected outcome.
        """
        headers = {"Content-Type": "application/json", "X-Reviewer": reviewer}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers.update(extra or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        try:
            return resp.status, json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return resp.status, data


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
        self.assertEqual(body["error"], "live synchronization requires confirm=true")

    def test_static_index_is_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.port}/", timeout=5) as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("Copyright &amp; License Review", html)

    def test_confirm_and_reassign_over_http(self):
        _, queue = self.server.call("GET", f"/api/mappings/{self.mapping_id}/components?limit=200")
        matched = next(i for i in queue["items"] if i["status"] != "unmatched")
        orphan = next(i for i in queue["items"] if i["status"] == "unmatched" and i["pl_name"])

        status, body = self.server.call("POST", f"/api/components/{orphan['id']}/confirm", {})
        self.assertEqual(status, 400)                      # ValueError -> 400, not a 500
        self.assertIn("both sides", body["error"])

        status, body = self.server.call("POST", f"/api/components/{matched['id']}/confirm", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("review_status", body)

        status, body = self.server.call("POST", f"/api/components/{matched['id']}/reassign",
                                        {"sl_key": "does-not-exist"})
        self.assertEqual(status, 400)
        self.assertIn("not in current snapshot", body["error"])

        status, body = self.server.call("POST", f"/api/components/{matched['id']}/reassign",
                                        {"sl_key": "extra-lib-0.1.0"})
        self.assertEqual(status, 200)
        _, detail = self.server.call("GET", f"/api/components/{matched['id']}")
        self.assertEqual(detail["match"]["method"], "manual")
        self.assertEqual(detail["match"]["sl_name"], "extra-lib")

    def test_overview_and_tools_over_http(self):
        status, out = self.server.call("GET", f"/api/mappings/{self.mapping_id}/overview")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(out), ["counts", "mapping", "recent_sync", "sides"])
        self.assertIn("matched", out["counts"])
        status, out = self.server.call("GET", "/api/tools")
        self.assertEqual(status, 200)
        tools = {t["tool"] for t in out["items"]}
        self.assertTrue({"fossology", "blackduck", "mock-pl"} <= tools)
        self.assertTrue(all({"roles", "description", "deploy"} <= set(t) for t in out["items"]))
        status, out = self.server.call("GET", "/api/sync/jobs?mapping_id=%d&limit=5" % self.mapping_id)
        self.assertEqual(status, 200)
        self.assertEqual(out["items"], [])


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
            self.assertTrue(payload["error"].endswith(" is required"), payload["error"])

    def test_non_numeric_id_is_400_with_a_clean_message(self):
        status, payload = self.server.call("POST", "/api/sync/jobs", {"mapping_id": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "mapping_id must be an integer")


class ErrorMappingTests(unittest.TestCase):
    """Every branch of the error mapping in clreview/api.py, over the real server."""

    def setUp(self):
        self.db = DB(":memory:")
        seed_demo(self.db)
        self.server = Server(self.db, token="test-token")
        conns = self.server.call("GET", "/api/connections", token="test-token")[1]["items"]
        self.sl_id = [c for c in conns if c["role"] == "sl"][0]["id"]
        self.connector = self.server.httpd.api.service.connector_for(self.sl_id)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def scans(self):
        return self.server.call("GET", f"/api/connections/{self.sl_id}/scans", token="test-token")

    def test_unknown_route_is_404(self):
        status, body = self.server.call("GET", "/api/does-not-exist", token="test-token")
        self.assertEqual(status, 404)
        self.assertIn("no route", body["error"])

    def test_unknown_match_is_404(self):
        status, body = self.server.call("GET", "/api/components/999999", token="test-token")
        self.assertEqual(status, 404)
        self.assertIn("not found", body["error"])

    def test_wrong_token_is_401(self):
        status, body = self.server.call("GET", "/api/connections", token="not-the-token")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "authentication required"})

    def test_invalid_json_body_is_400(self):
        status, body = self.server.raw("POST", "/api/connections", body=b"{not json", token="test-token")
        self.assertEqual(status, 400)
        self.assertIn("invalid JSON body", body["error"])

    def test_body_too_large_is_400(self):
        with mock.patch.object(api_module, "MAX_BODY", 16):
            status, body = self.server.call("POST", "/api/connections", {"name": "x" * 64}, token="test-token")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "request body too large")

    def test_unsupported_operation_is_409(self):
        with mock.patch.object(self.connector, "list_scans",
                               side_effect=UnsupportedOperation("scan discovery is not supported here")):
            status, body = self.scans()
        self.assertEqual(status, 409)
        self.assertIs(body["unsupported"], True)
        self.assertIn("not supported", body["error"])

    def test_connector_failure_is_502_with_the_upstream_status(self):
        with mock.patch.object(self.connector, "list_scans",
                               side_effect=ConnectorError("upstream FOSSology answered 503", status=503)):
            status, body = self.scans()
        self.assertEqual(status, 502)
        self.assertEqual(body["status"], 503)
        self.assertIs(body["unsupported"], False)

    def test_unhandled_error_is_500_and_leaks_nothing(self):
        with mock.patch.object(self.connector, "list_scans",
                               side_effect=RuntimeError("upstream token=super-secret")):
            with self.assertLogs("clreview.api", "ERROR"):
                status, body = self.scans()
        self.assertEqual(status, 500)
        self.assertEqual(body, {"error": "internal server error"})


class StaticFileTests(unittest.TestCase):
    """The static handler serves the bundle and nothing outside it."""

    def setUp(self):
        self.db = DB(":memory:")
        self.server = Server(self.db)          # no auth: these are public assets

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_index_and_assets_are_served(self):
        self.assertEqual(self.server.raw("GET", "/")[0], 200)
        self.assertEqual(self.server.raw("GET", "/app.js")[0], 200)

    def test_traversal_and_missing_files_are_404(self):
        for path in ("/../../etc/passwd", "/../../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/nope.js"):
            status, body = self.server.raw("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body, b"", path)

    def test_a_sibling_of_the_web_root_is_not_served(self):
        """startswith(WEB_ROOT) is a prefix match; a sibling named web_* must not escape it."""
        root = tmpdir()
        os.makedirs(os.path.join(root, "web"))
        os.makedirs(os.path.join(root, "web_secret"))
        with open(os.path.join(root, "web", "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<html>ok</html>")
        secret = os.path.join(root, "web_secret", "leak.txt")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("TOP-SECRET")
        with mock.patch.object(api_module, "WEB_ROOT", os.path.join(root, "web")):
            status, body = self.server.raw("GET", "/../web_secret/leak.txt")
        self.assertEqual(status, 404)
        self.assertNotIn(b"TOP-SECRET", body if isinstance(body, bytes) else json.dumps(body).encode())


class ErrorBodyTests(unittest.TestCase):
    """A11 + A12: what a failing request says, and what an unread body means."""

    def setUp(self):
        self.db = DB(":memory:")
        self.mapping_id = seed_demo(self.db)["mapping_id"]
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def snapshot_count(self):
        return self.db.one("SELECT COUNT(*) AS n FROM snapshots")["n"]

    def refreshed_events(self):
        return self.db.one("SELECT COUNT(*) AS n FROM audit WHERE action='snapshot.refreshed'")["n"]

    def test_404_body_is_a_plain_message_not_a_python_repr(self):
        status, body = self.server.call("GET", "/api/components/999999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "match 999999 not found")
        self.assertNotIn("KeyError", body["error"])
        self.assertNotIn("'", body["error"])

    def test_another_404_site_is_equally_clean(self):
        status, body = self.server.call("GET", "/api/sync/jobs/4242")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "sync job 4242 not found")

    def test_chunked_body_is_rejected_not_treated_as_empty(self):
        """A chunked body is refused, never read as an empty ``{}``.

        Refusal has two legal shapes and only timing decides which one the client sees: the server
        answers 411, or it hangs up while the client is still writing the terminating chunk and the
        write itself fails (``BrokenPipeError``; a closed socket can also surface as
        ``ConnectionResetError`` - which ``http.client.RemoteDisconnected`` subclasses - or
        ``ConnectionAbortedError``). Both mean "not accepted", so neither is a failure here.

        The assertion that matters is checked in both branches: the body was not parsed as ``{}``
        and no refresh ran. A server that starts accepting chunked bodies fails this test whether
        or not the client managed to finish sending.
        """
        path = f"/api/mappings/{self.mapping_id}/refresh"
        before = (self.snapshot_count(), self.refreshed_events())
        hung_up = None
        try:
            status, body = self.server.raw("POST", path, body=iter([b"{}"]))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            hung_up = type(exc).__name__
            status, body = None, {}
        if hung_up is None:
            self.assertEqual(status, 411, f"a chunked body was accepted: {status} {body}")
            self.assertEqual(body.get("error"),
                             "Content-Length required: chunked request bodies are not supported")
        self.assertEqual((self.snapshot_count(), self.refreshed_events()), before,
                         f"the refresh ran ({hung_up or f'{status} {body}'}): the chunked body "
                         "was treated as an empty {}")

    def test_a_genuinely_bodyless_post_still_works(self):
        status, body = self.server.raw("POST", f"/api/mappings/{self.mapping_id}/refresh")
        self.assertEqual(status, 200, body)
        self.assertGreater(self.snapshot_count(), 0)


class RouteTableTests(unittest.TestCase):
    """The table is the whole HTTP surface: every entry is wired, every mismatch is a 404."""

    def setUp(self):
        self.db = DB(":memory:")
        seed_demo(self.db)
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_every_route_entry_is_wired(self):
        api = self.server.httpd.api
        seen = set()
        for method, pattern, handler in api_module.ROUTES:
            with self.subTest(route=f"{method} {pattern.pattern}"):
                self.assertTrue(callable(getattr(api, handler, None)), handler)
                self.assertNotIn((method, pattern.pattern), seen)
                seen.add((method, pattern.pattern))

    def test_method_mismatch_is_404(self):
        for method, path in (("GET", "/api/components/1/decisions"),
                             ("POST", "/api/components/1"),
                             ("GET", "/api/mappings/1/refresh"),
                             ("POST", "/api/sync/jobs/1")):
            status, body = self.server.call(method, path, {})
            self.assertEqual(status, 404, (method, path, body))
            self.assertEqual(body["error"], f"no route for {method} {path}")


if __name__ == "__main__":
    unittest.main()
