# Plan — Attack step 5 (C3, C4, C6): cover the untested paths

**Repo:** `/home/dinesh/repos/foss_data_reviewer` · **HEAD:** `b7fae62` (step 4: matching scale)
**Source of truth:** `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md` → findings **C3, C4, C6**
**Order:** C3 → C4 → C6, as the scout ordered them. Three commits, one per finding.

Everything below was measured or run in a scratch copy of this repo before writing it. Two things the scout did
not know turned out to matter, and both are called out in place:

1. **C4 exposes a real defect** in the `_static` guard (a sibling-directory escape that returns 200 + file
   contents). The plan carries the one-line fix, as the brief allows.
2. **C6 is bigger than installing chromium.** `web_smoke.mjs` **fails on a fresh database** — it passes locally
   only because of a leftover `clreview.db` (gitignored). Installing chromium in CI without fixing the smoke's
   own setup would turn CI red on the first run.

---

## 0. Baseline, measured on this machine

```
uv run pytest -q --ignore=adws     → 117 passed, coverage 77.02% (gate 75)
uv run pytest tests/test_web_smoke.py -q --ignore=adws  → 1 passed in 35.1 s  (local, stale clreview.db)
```

Zero-coverage targets (statements executed / missing, from `--cov-report=json` + an AST walk of each function):

| target | before |
|---|---|
| `review.py:466 confirm_match` | 1 / 6 |
| `review.py:476 reassign` | 1 / 13 |
| `service.py:142 tools` | 1 / 1 |
| `service.py:359 overview` | 1 / 10 |
| `sync.py:295 list_jobs` | 1 / 4 |
| `fossology.py:207 _upload_inventory` | 1 / 48 |
| `fossology.py:304 _upload_name` | 1 / 5 |
| `fossology.py:340 _aggregate_copyrights` | 1 / 14 |
| `fossology.py:356 _generic_text` | 1 / 9 |
| `fossology.py:367 _item_map` | 1 / 13 |
| `fossology.py:382 _walk` | 1 / 15 |

File totals: `clreview/connectors/fossology.py` **45.96%**, `clreview/api.py` **80.5%**.

With the tests in this plan prototyped, the same measurement gives **82.16% total** and every function above
fully executed (`_upload_inventory` 48/48, `reassign` 14/14, `confirm_match` 7/7, `overview` 11/11,
`list_jobs` 5/5). Expect the committed number in the **82-83%** range; report the actual figure.

Error-mapping statuses were confirmed against a live server (probe, not guessed):

```
GET  /api/does-not-exist          404  {"error": "'no route for GET /api/does-not-exist'"}
GET  /api/components/999999       404  {"error": "'match 999999 not found'"}
GET  /api/connections  wrong token 401 {"error": "authentication required"}
POST invalid JSON body            400  {"error": "invalid JSON body: ..."}
POST body > MAX_BODY              400  {"error": "request body too large"}
GET  scans, UnsupportedOperation  409  {"error": "...", "unsupported": true}
GET  scans, ConnectorError(503)   502  {"error": "...", "status": 503, "unsupported": false}
GET  scans, RuntimeError          500  {"error": "internal server error"}   (traceback goes to the logger only)
```

---

## 1. C3 — the write paths nobody exercises (`tests/test_review.py`, `tests/test_sync.py`, `tests/test_e2e.py`)

### 1a. `confirm_match` and `reassign` at service level — append to `tests/test_review.py`

`tests/test_review.py` already has `base_components()` with one matched pair; confirmation and reassignment need
a **second component on each side** so there is an orphan to refuse and a real key to reassign onto. Add a new
fixture builder and class (verified passing):

```python
def pair_components():
    """Two per side: one matched pair, one orphan on each side, and a copyright variant to decide."""
    pl = [pl_component(name="zlib", version="1.2.11", purl="pkg:github/madler/zlib@1.2.11",
                       copyrights=[{"text": "Copyright (C) 1995-2017 Jean-loup Gailly", "origin": "origin"}]),
          pl_component(name="left-pad", version="1.0.0")]
    sl = [{"external_id": "zlib-1.2.11", "name": "zlib", "version": "1.2.11",
           "purl": "pkg:github/madler/zlib@1.2.11",
           "copyrights": [{"text": "Copyright  1995-2017  Jean-loup Gailly", "source_path": "zlib/zutil.c"}]},
          {"external_id": "other-2.0", "name": "other", "version": "2.0"}]
    return pl, sl


class ConfirmReassignTests(unittest.TestCase):
    """confirm_match and reassign are the two write paths with no test at all."""

    def setUp(self):
        self.dir = tmpdir()
        pl, sl = pair_components()
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.review, self.sync = make_review(self.svc)
        items = self.review.list_components(self.mid)["items"]
        self.matched = next(i for i in items if i["status"] != "unmatched")["id"]
        self.orphan = next(i for i in items if i["status"] == "unmatched" and i["pl_name"])["id"]

    def rows(self):
        return {r["id"]: r for r in self.db.query("SELECT * FROM matches WHERE mapping_id=?", (self.mid,))}

    def test_confirm_records_reviewer_and_time(self):
        out = self.review.confirm_match(self.matched, "alice")
        row = self.rows()[self.matched]
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["confirmed_by"], "alice")
        self.assertTrue(row["confirmed_at"])
        self.assertIn("review_status", out)                      # recompute ran as part of confirming
        actions = [r["action"] for r in self.db.query("SELECT * FROM audit WHERE entity_id=?", (self.matched,))]
        self.assertIn("match.confirmed", actions)

    def test_confirm_requires_both_sides(self):
        with self.assertRaises(ValueError) as ctx:
            self.review.confirm_match(self.orphan, "alice")
        self.assertIn("both sides", str(ctx.exception))
        self.assertNotEqual(self.rows()[self.orphan]["status"], "confirmed")

    def test_reassign_rewrites_the_pair_and_recomputes(self):
        out = self.review.reassign(self.matched, sl_key="other-2.0", reviewer="bob")
        row = self.rows()[self.matched]
        self.assertEqual((row["pl_key"], row["sl_key"]), ("zlib", "other-2.0"))
        self.assertEqual((row["sl_name"], row["sl_version"]), ("other", "2.0"))
        self.assertEqual((row["pl_name"], row["pl_version"]), ("zlib", "1.2.11"))
        self.assertEqual((row["method"], row["confidence"], row["status"]), ("manual", 1.0, "confirmed"))
        self.assertEqual(row["confirmed_by"], "bob")
        self.assertEqual(out["review_status"], "review_required")   # recomputed against the new pair
        audit = self.db.query("SELECT * FROM audit WHERE action='match.reassigned' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(audit["actor"], "bob")

    def test_reassign_rejects_a_key_that_is_not_in_the_snapshot(self):
        with self.assertRaises(ValueError) as ctx:
            self.review.reassign(self.matched, sl_key="does-not-exist")
        self.assertIn("not in current snapshot", str(ctx.exception))
        self.assertEqual(self.rows()[self.matched]["sl_key"], "zlib-1.2.11")
        with self.assertRaises(ValueError):
            self.review.reassign(self.matched, pl_key="nope")

    def test_reassign_can_move_the_pl_side(self):
        self.review.reassign(self.matched, pl_key="left-pad", reviewer="bob")
        self.assertEqual(self.rows()[self.matched]["pl_name"], "left-pad")
```

Notes for whoever writes it: queue items from `list_components` carry `pl_name`/`sl_name`, **not** `pl_key` —
that is why the orphan is found by `pl_name`. `reassign` sets `method='manual'`, `confidence=1.0`,
`status='confirmed'` and rewrites all four name/version columns in one statement (`clreview/review.py:490-498`),
which is what the assertions pin.

### 1b. `overview` — same file, same class or a small one next to it

`service.overview` is 10 statements and has never run. It needs a `sync_jobs` row to prove `recent_sync`;
inserting the row directly is cheaper than driving a whole sync and keeps the test about `overview`:

```python
    def test_overview_reports_sides_counts_and_recent_jobs(self):
        out = self.svc.overview(self.mid)
        self.assertEqual(out["mapping"]["id"], self.mid)
        for side in ("pl", "sl"):
            self.assertEqual(out["sides"][side]["status"], "ok")
            self.assertEqual(out["sides"][side]["count"], 2)
            self.assertTrue(out["sides"][side]["snapshot_id"])
        self.assertEqual(out["counts"]["matched"], 1)
        self.assertEqual(out["counts"]["unmatched"], 2)
        self.assertEqual(out["recent_sync"], [])
        self.db.execute("INSERT INTO sync_jobs(mapping_id,dry_run,status,created_by,created_at)"
                        " VALUES(?,1,'dry_run','bob',?)", (self.mid, "2026-01-01T00:00:00+00:00"))
        self.assertEqual([j["id"] for j in self.svc.overview(self.mid)["recent_sync"]], [1])
```

### 1c. `list_jobs` — append to `tests/test_sync.py`

`create_job` refuses when nothing is approved (`sync.py:128`), so the test must approve something first —
which also means this test exercises a real job rather than a hand-inserted row:

```python
class ListJobsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.svc, self.db, self.mid = make_service(self.dir, *pair_components())
        self.review, self.sync = make_review(self.svc)
        item = next(i for i in self.review.list_components(self.mid)["items"] if i["discrepancy_count"])
        detail = self.review.detail(item["id"])
        for row in detail["comparison"]["copyrights"]:
            if row["kind"] in ("variant", "sl_only", "pl_only"):
                self.review.decide(item["id"], "copyright", row["key"], "approve_sl", reviewer="bob")
        self.review.set_review_state(item["id"], "approved_for_sync", "bob")

    def test_list_jobs_filters_orders_and_limits(self):
        first = self.sync.create_job(self.mid, dry_run=True, actor="bob")
        second = self.sync.create_job(self.mid, dry_run=True, actor="bob")
        self.assertEqual([j["id"] for j in self.sync.list_jobs(self.mid)], [second["id"], first["id"]])
        self.assertEqual([j["id"] for j in self.sync.list_jobs()], [second["id"], first["id"]])
        self.assertEqual([j["id"] for j in self.sync.list_jobs(self.mid, limit=1)], [second["id"]])
        self.assertEqual(self.sync.list_jobs(999), [])
        self.assertIs(self.sync.list_jobs(self.mid)[0]["dry_run"], True)
```

`tests/test_sync.py` imports whatever it does not already have from `helpers` (`make_review`, `tmpdir`) and
imports `pair_components` from `test_review` (`from test_review import pair_components` — flat `tests/` layout,
`sys.path` already includes it).

### 1d. One HTTP call each — append to `tests/test_e2e.py`

The routes exist (`clreview/api.py:85-90`); nothing calls them. Demo SL keys worth using:
`zlib-1.2.11`, `zlib-ng-2.1.2`, `extra-lib-0.1.0` (the last one is the unmatched SL component).

```python
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
```

---

## 2. C3 — FOSSology evidence assembly (`tests/test_connectors.py`)

`_upload_inventory` and its four helpers (`_item_map`, `_walk`, `_aggregate_copyrights`, `_generic_text`, plus
`_upload_name`) are 104 statements at 45.96% file coverage, and they are the only code that decides what
"evidence" means for the whole platform. Test them with a fake that answers the REST paths — the existing
`FossologyGroupingTests` only ever calls `_group` directly.

Add to `tests/test_connectors.py` (verified passing, 6 tests):

```python
FOSS_LICENSE_TEXT = "Copyright (C) 1995-2017 Jean-loup Gailly and Mark Adler\n\nThis software is provided 'as-is'."
FOSS_AGGREGATE = "Copyright (C) 1995-2017 Jean-loup Gailly and Mark Adler"


class FakeFossology(FossologyConnector):
    """Answers the FOSSology REST paths _upload_inventory needs, from a dict of path -> payload."""

    def __init__(self, routes, texts=None):
        super().__init__("https://fossy.example",
                         {"agents": "nomos", "max_license_files": 5, "generic_text_limit": 2}, {"token": "t"})
        self.routes = routes
        self.texts = texts or {}
        self.calls = []

    def _get(self, url, page=None):
        path = url.split("fossy.example", 1)[1].split("?")[0]
        self.calls.append(path)
        if path not in self.routes:
            raise ConnectorError(f"GET {url} failed with 404", status=404)
        return self.routes[path], {"x-total-pages": "1"}

    def _get_text(self, url):
        path = url.split("fossy.example", 1)[1].split("?")[0]
        self.calls.append(path)
        if path not in self.texts:
            raise ConnectorError(f"GET {url} failed with 404", status=404)
        return self.texts[path]


def foss_routes():
    return {
        "/uploads/7/licenses": [
            {"filePath": "zlib-1.2.11/zutil.c",
             "findings": {"scanner": ["Zlib"], "copyright": ["Copyright (C) 1995-2017 Jean-loup Gailly"]}},
            {"filePath": "zlib-1.2.11/inflate.c",
             "findings": {"scanner": ["Zlib"], "copyright": ["Copyright (C) 1995-2017 Jean-loup Gailly"]}},
            {"filePath": "zlib-1.2.11/LICENSE", "findings": {"scanner": ["Zlib"], "copyright": []}},
        ],
        "/uploads/7/summary": {"uploadName": "zlib-1.2.11.tar.gz"},
        "/uploads/7/copyrights": [{"copyright": FOSS_AGGREGATE, "filePath": ["zlib-1.2.11/zutil.c"]}],
        "/uploads/7/topitem": {"message": 1},
        "/uploads/7/item/1/tree/view": [{"fileDetails": {"fileName": "zlib-1.2.11", "id": 2, "isContainer": True}}],
        "/uploads/7/item/2/tree/view": [
            {"fileDetails": {"fileName": "LICENSE", "id": 4}},
            {"fileDetails": {"fileName": "zutil.c", "id": 5}},
            {"fileDetails": {"fileName": "no-id"}},
        ],
        "/license/Zlib": {"text": "The zlib License text"},
    }
```

and the tests:

```python
class FossologyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeFossology(foss_routes(), {"/uploads/7/item/4/view": FOSS_LICENSE_TEXT})

    def test_assembles_one_component_with_evidence(self):
        components = self.conn._upload_inventory("7")
        self.assertEqual(len(components), 1)
        c = components[0]
        self.assertEqual((c.name, c.version), ("zlib", "1.2.11"))
        self.assertEqual(c.metadata["upload_id"], "7")
        self.assertEqual(sorted(c.source_files),
                         ["zlib-1.2.11/LICENSE", "zlib-1.2.11/inflate.c", "zlib-1.2.11/zutil.c"])
        self.assertEqual([l.name for l in c.licenses], ["Zlib"])
        self.assertEqual(c.licenses[0].evidence_paths,
                         ["zlib-1.2.11/zutil.c", "zlib-1.2.11/inflate.c", "zlib-1.2.11/LICENSE"])
        self.assertEqual([t.text for t in c.copyrights],
                         ["Copyright (C) 1995-2017 Jean-loup Gailly", FOSS_AGGREGATE])
        self.assertEqual(c.copyrights[1].source_path, "zlib-1.2.11/zutil.c")   # from the aggregate endpoint
        source = [t for t in c.license_texts if t.kind == "source_file"]
        generic = [t for t in c.license_texts if t.kind == "generic_metadata"]
        self.assertEqual(len(source), 1)
        self.assertEqual(source[0].file_path, "zlib-1.2.11/LICENSE")
        self.assertEqual(source[0].content_hash, sha256(FOSS_LICENSE_TEXT))
        self.assertTrue(source[0].is_license_file)
        self.assertIn("item 4", source[0].provenance)
        self.assertEqual([g.text for g in generic], ["The zlib License text"])
        self.assertIn("/license/Zlib", generic[0].provenance)

    def test_item_map_and_generic_text_are_cached(self):
        self.conn._upload_inventory("7")
        before = list(self.conn.calls)
        self.conn._item_map("7")
        self.conn._generic_text("Zlib")
        self.assertEqual(self.conn.calls, before)                       # nothing refetched
        self.assertEqual(self.conn.calls.count("/uploads/7/topitem"), 1)
        self.assertEqual(self.conn.calls.count("/license/Zlib"), 1)

    def test_missing_optional_endpoints_degrade_instead_of_failing(self):
        routes = foss_routes()
        del routes["/uploads/7/copyrights"], routes["/license/Zlib"], routes["/uploads/7/summary"]
        conn = FakeFossology(routes, {"/uploads/7/item/4/view": FOSS_LICENSE_TEXT})
        c = conn._upload_inventory("7")[0]
        self.assertEqual([t.text for t in c.copyrights], ["Copyright (C) 1995-2017 Jean-loup Gailly"])
        self.assertEqual([t.kind for t in c.license_texts], ["source_file"])
        self.assertEqual(conn._upload_name("7"), "upload-7")

    def test_unreadable_license_file_is_skipped(self):
        c = FakeFossology(foss_routes(), {})._upload_inventory("7")[0]
        self.assertEqual([t.kind for t in c.license_texts], ["generic_metadata"])

    def test_file_missing_from_item_map_is_skipped(self):
        conn = FakeFossology(foss_routes(), {"/uploads/7/item/4/view": FOSS_LICENSE_TEXT})
        conn._item_maps["7"] = {"zlib-1.2.11/other.c": 9}
        c = conn._upload_inventory("7")[0]
        self.assertEqual([t.kind for t in c.license_texts], ["generic_metadata"])

    def test_walk_stops_at_the_depth_cap(self):
        conn = FakeFossology(foss_routes(), {})
        for i in range(60):
            conn.routes[f"/uploads/7/item/{100 + i}/tree/view"] = [
                {"fileDetails": {"fileName": f"d{i}", "id": 101 + i, "isContainer": True}}]
        out: dict[str, int] = {}
        conn._walk("7", 100, "", out)
        self.assertLess(len(out), 60)
```

What each one is really pinning: grouping and name/version split, per-file copyright de-duplication plus the
aggregate endpoint merged in by path overlap, `content_hash` on the fetched file text (that hash is what the
comparison layer matches on), generic text labelled as metadata and capped by `generic_text_limit`, both caches,
and the three degradation paths (`/copyrights`, `/license/{name}`, item view) that must never fail a whole
inventory. `sha256` is already imported in `tests/test_connectors.py`; `ConnectorError` and `FossologyConnector`
are too.

---

## 3. C4 — the HTTP error mapping, and one real defect (`tests/test_e2e.py`, one line in `clreview/api.py`)

### 3a. The defect the traversal test exposes — fix it

`Handler._static` (`clreview/api.py:265-270`) guards with `full.startswith(WEB_ROOT)`. That is a **prefix**
match, not a path containment check, so any sibling whose name starts with `web` escapes. Measured against a
`WEB_ROOT` of `<tmp>/web` with a sibling `<tmp>/web_secret/leak.txt`:

```
GET /../web_secret/leak.txt   →  200, body "TOP-SECRET"      # before
GET /../web_secret/leak.txt   →  404                          # after the fix
```

The shipped layout has no `clreview/web*` sibling today, so this is latent, not live — but it is a one-character
class of bug in a security check and the fix is one line:

```python
# clreview/api.py, in _static
-        if not full.startswith(WEB_ROOT) or not os.path.isfile(full):
+        if not (full == WEB_ROOT or full.startswith(WEB_ROOT + os.sep)) or not os.path.isfile(full):
```

Verified: classic traversal still 404, `/` and `/app.js` still 200, sibling escape now 404. Say so in the commit
body — this is the only source change in the whole step.

### 3b. Tests — append to `tests/test_e2e.py`

Two small additions to the existing `Server` harness first (`urllib` normalises `..` and always builds its own
headers, so raw bytes need `http.client`):

```python
import http.client
from unittest import mock

from clreview import api as api_module
from clreview.connectors.base import ConnectorError
from clreview.connectors.http import UnsupportedOperation
```

```python
    def raw(self, method, path, body=None, token=None, reviewer="alice"):
        """Send a path or body verbatim - urllib normalises '..' and rewrites headers."""
        headers = {"Content-Type": "application/json", "X-Reviewer": reviewer}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return resp.status, data
```

Then the mapping tests. The connector instance is taken from the server's own cache
(`api.service.connector_for(id)`) and one method is patched, so the request travels the real route, through the
real `Service`, into the real exception handler:

```python
class ErrorMappingTests(unittest.TestCase):
    """Every branch of the error mapping in clreview/api.py:252-262, over the real server."""

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
        status, body = self.server.raw("POST", "/api/connections", body=b"{not json",
                                       token="test-token")
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
        root = tempfile.mkdtemp()
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
```

`tests/test_e2e.py` needs `os`, `tempfile` and `http.client` added to its imports.

---

## 4. C6 — make the browser smoke actually run, and make it not flake

### 4a. The smoke test is broken on a fresh database — fix that first

Measured on a clean checkout copy (no `clreview.db`), the **current** `web_smoke.mjs` fails:

```
FAIL  history shows pending writes — demo has nothing approved
FAIL  overview job link opens the job
2 FAILED
```

Cause: the setup does
`comps.items.find((c) => c.review_status === 'reviewed') || comps.items[0]` and then approves that. A freshly
seeded demo has **no** `reviewed` component (all are `review_required`/`not_reviewed`), so it falls back to
`items[0]` — an unmatched row — and the approve call is refused. Locally the leftover `clreview.db` (gitignored)
already had a reviewed component, which is the only reason the test has been green.

Two changes, both in `web_smoke.mjs`:

1. **Hermetic database.** Create the temp profile first and pass `--db` inside it, so a leftover repo `clreview.db`
   can never decide what the demo looks like:
   ```js
   const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cl-smoke-'));
   const server = spawn(PY, ['-m', 'clreview.server', '--demo', '--port', String(port),
     '--db', path.join(profile, 'smoke.db')], { stdio: 'ignore' });
   ```
2. **Decide a component through the API before approving it** (replacing the `toApprove` line). Verified to
   reach `approved_for_sync` on a fresh demo DB:
   ```js
   const approveMe = comps.items.find((c) => c.status !== 'unmatched' && c.discrepancy_count > 0) || comps.items[0];
   const det = await api('GET', `/api/components/${approveMe.id}`);
   const decide = (field, entry_key) => api('POST', `/api/components/${approveMe.id}/decisions`,
     { field, entry_key, action: 'approve_sl' });
   const COPYRIGHT_KINDS = ['variant', 'sl_only', 'pl_only'];
   const LICENSE_KINDS = ['spdx_diff', 'sl_only', 'pl_only'];
   for (const r of det.comparison.copyrights) if (COPYRIGHT_KINDS.includes(r.kind)) await decide('copyright', r.key);
   for (const r of det.comparison.licenses) if (LICENSE_KINDS.includes(r.kind)) await decide('license_name', r.key);
   const ltx = det.comparison.license_text;
   if (ltx && ltx.recommendation === 'replace_with_source' && ltx.preferred) {
     await decide('license_text', ltx.preferred.content_hash);
   }
   const approved = await api('POST', `/api/components/${approveMe.id}/review`, { state: 'approved_for_sync' });
   ok('setup: a demo component reaches approved-for-sync', approved.review_status === 'approved_for_sync',
     approved.error || `${approveMe.pl_name || approveMe.sl_name}`);
   ```
   Do not name the variable `target` — the CDP-target discovery loop below already declares `let target`.

### 4b. Replace every fixed sleep with a condition poll

The scout asked for this to stop flakes; it turned out to be load-bearing, because the app **re-renders a view
when its data arrives**. A click that lands before the last render is a click on a node the app is about to
throw away — that is exactly how the polled version failed on the first attempt (`sync-confirm` change and
`sync-dry` click silently did nothing). So the waits need two things: a condition **and** a quiescence check.

Add after the `ev`/`send` helpers, and install the request counter before the first navigation:

```js
  await send('Page.enable'); await send('Runtime.enable');
  // Count in-flight requests from the first document on: the app re-renders a view when its data
  // arrives, so a click that lands before the last render is a click on a node about to be replaced.
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    window.__pending = 0;
    const realFetch = window.fetch;
    window.fetch = (...a) => { window.__pending++; return realFetch(...a).finally(() => { window.__pending--; }); };
  ` });
```

```js
  // Poll a page condition instead of sleeping a fixed time: a slow runner waits, it does not flake.
  const until = async (label, predicate, ms = 10000) => {
    const started = Date.now();
    for (;;) {
      if (await ev(`(() => { try { return !!(${predicate}); } catch { return false; } })()`)) return true;
      if (Date.now() - started > ms) { ok(`wait: ${label}`, false, `timed out after ${ms}ms`); return false; }
      await sleep(100);
    }
  };
  // quiet: no request in flight for three polls running, so the render the last response triggered has run
  const quiet = async (label = 'network', ms = 10000) => {
    const started = Date.now();
    let still = 0;
    for (;;) {
      still = (await ev(`window.__pending === 0 ? 1 : 0`)) ? still + 1 : 0;
      if (still >= 3) return true;
      if (Date.now() - started > ms) { ok(`wait: ${label} settled`, false, `still busy after ${ms}ms`); return false; }
      await sleep(100);
    }
  };
  const go = async (hash, ready = `document.querySelector('.view:not(.hidden) h1')`) => {
    await send('Page.navigate', { url: base + hash });
    await until(`rendered ${hash}`, `location.hash === ${JSON.stringify(hash.slice(1))} && ${ready}`);
    await quiet(`view ${hash}`);
  };
```

Then, for every action: `ev(click)` → `quiet(...)` → `until(the condition the next assertion needs)` → `ok(...)`.
The `ok()` calls stay exactly as they are; a timeout reports itself as a named `FAIL wait: …` instead of hanging.

Replacement table (left = today, right = the wait to put in its place):

| today | becomes |
|---|---|
| `go('/#/component/7')` + `sleep(1500)` | `go('/#/component/7', \`document.getElementById('note-body')\`)` |
| `go('/#/queue')` | `go('/#/queue', \`document.querySelectorAll('td[data-label=discrepancies]').length > 0\`)` |
| `go('/#/deploy')` | `go('/#/deploy', \`document.getElementById('dp-run')\`)` |
| add-note click (no reviewer) + `sleep(400)` | `quiet('reviewer gate')` + `until('inline reviewer error', \`!document.getElementById('reviewer-error').hidden\`)` |
| reviewer name input + `sleep(150)` | `quiet('reviewer name')` + `until('reviewer error cleared', \`document.getElementById('reviewer-error').hidden === true && !document.getElementById('reviewer').hasAttribute('aria-invalid')\`)` |
| add-note click (named) + `sleep(1600)` | `quiet('note write')` + `until('note written', \`${NOTES} > ${before}\`)` |
| queue search apply + `sleep(1200)` | `quiet('queue search')` + `until('search applied', \`document.getElementById('q-clear').disabled === false\`)` |
| q-clear click + `sleep(1200)` | `quiet('queue clear')` + `until('filters reset', \`document.getElementById('q-search').value === '' && document.getElementById('q-clear').disabled === true\`)` |
| `sync-confirm` change + `sleep(200)` | `quiet('sync approval')` + `until('execute enabled', \`document.getElementById('sync-live').disabled === false\`)` |
| `sync-dry` click + `sleep(2500)` | `quiet('dry run')` + `until('dry-run job detail', \`((document.querySelector('#job-detail h2') \|\| {}).textContent \|\| '').includes('Job #')\`, 20000)` |
| overview job link click + `sleep(1500)` | `quiet('job link')` + `until('job detail from overview', \`((document.querySelector('#job-detail h2') \|\| {}).textContent \|\| '').includes(${JSON.stringify(jobId)})\`, 20000)` |
| `dp-preview` click + `sleep(1500)` | `quiet('deploy preview')` + `until('deploy preview rows', \`document.querySelectorAll('#dp-report tbody tr').length > 0\`, 20000)` |
| `dp-confirm` change + `sleep(200)` | `quiet('deploy approval')` + `until('upload enabled after approval', \`document.getElementById('dp-run').disabled === false\`)` |
| `dp-folder` = 'smoke-changed' + `sleep(200)` | `quiet('target change')` + `until('target change invalidates preview', \`document.getElementById('dp-run').disabled === true\`)` |
| `dp-folder` = '' + `sleep(200)` | `quiet('target cleared')` + `until('target cleared', \`document.getElementById('dp-folder').value === ''\`)` |
| `dp-run` click + `sleep(2000)` | `quiet('live upload', 30000)` + `until('live upload report', \`[...document.querySelectorAll('#dp-report .stat')].some((s) => /uploaded/.test(s.textContent))\`, 30000)` |
| `dp-mapping` click + `sleep(3500)` | `untilApi('mapping for the deployed folder', async () => (await folderMaps()) === maps0 + 1)` |

`untilApi` is the same loop on the node side (the deployed-mapping check is an API assertion, not a DOM one):

```js
  const untilApi = async (label, test, ms = 20000) => {
    const started = Date.now();
    for (;;) {
      if (await test()) return true;
      if (Date.now() - started > ms) { ok(`wait: ${label}`, false, `timed out after ${ms}ms`); return false; }
      await sleep(200);
    }
  };
```

Leave the two startup retry loops (`for (let i = 0; i < 80 ...)` polling `/api/health` and the CDP `/json/list`)
as they are — they are already condition polls.

Two traps I hit, so you don't:

* a `\d` inside a JS **template literal** is just `d`, so `` `/Job #\d+/` `` silently becomes `/Job #d+/`. Use
  `.includes('Job #')` in the predicate (the `ok()` assertion below it keeps its own regex, outside a template).
* `node --check tests/web_smoke.mjs` catches the syntax problems cheaply; run it before a 30 s browser run.

Measured result of 4b + 4a on a fresh database: **3 consecutive clean runs**, 35 checks, and the pytest wrapper
drops from **35.1 s to 7.6 s** because nothing waits on a clock any more.

### 4c. CI: install a browser and fail loudly if the smoke skips

`tests/test_web_smoke.py:16` skips when `node` or a chromium binary is missing. Add to the `test` job in
`.github/workflows/ci.yml`, between "Install" and "Tests":

```yaml
      - name: Browser for the web smoke test
        uses: browser-actions/setup-chrome@v2        # v2.2.0; puts google-chrome on PATH, which the skip check finds
      - name: Browser is really installed
        run: |
          node --version
          google-chrome --version
```

and make the test step prove the smoke ran instead of skipping (this is the part that keeps CI honest — a
silent skip is what hid this for the whole time the suite has existed):

```yaml
      - name: Tests
        run: |
          set -o pipefail
          uv run pytest -q --ignore=adws -rs 2>&1 | tee /tmp/pytest.log
          if grep -q "web smoke test needs node" /tmp/pytest.log; then
            echo "::error::web smoke test was skipped - no chromium on the runner"
            exit 1
          fi
```

`set -o pipefail` is required: without it, `pytest | tee` reports tee's exit status and a red suite turns green.
`-rs` is what prints the skip reason that `grep` looks for. Do the same in the `lint` job? No — one job runs the
suite. Running the smoke on all four matrix pythons costs ~8 s each now, so leave the matrix as it is.

If `browser-actions/setup-chrome` is ever unavailable, the fallback is
`sudo apt-get update && sudo apt-get install -y chromium` — but prefer the action; `chromium-browser` on
ubuntu-24.04 is a snap and snaps are not available on the hosted runners.

---

## 5. Files touched

| file | change |
|---|---|
| `tests/test_review.py` | `pair_components()`, `ConfirmReassignTests` (5 tests incl. `overview`) |
| `tests/test_sync.py` | `ListJobsTests` (1 test) |
| `tests/test_connectors.py` | `FakeFossology`, `foss_routes()`, `FossologyEvidenceTests` (6 tests) |
| `tests/test_e2e.py` | `Server.raw()`, `ErrorMappingTests` (8), `StaticFileTests` (3), confirm/reassign/overview/tools HTTP tests (3) |
| `tests/web_smoke.mjs` | hermetic `--db`, API-driven approval, `until`/`quiet`/`untilApi`, every fixed sleep replaced |
| `.github/workflows/ci.yml` | chrome install + "must not skip" guard |
| `clreview/api.py` | **one line** — `_static` containment check (§3a), the only source change |

No new dependencies. No change to `pyproject.toml`, `clreview/review.py`, `service.py`, `sync.py`,
`connectors/fossology.py` — the tests pass against the code as it is.

Three commits, in this order:
1. `Cover confirm_match, reassign, overview, list_jobs and the FOSSology evidence path`
2. `Exercise every HTTP error-mapping branch and close a static-path containment hole`
3. `Run the browser smoke test in CI: chromium, hermetic demo data, condition polls`

---

## 6. Verification

```bash
uv run pytest -q --ignore=adws                        # green, coverage gate; report the new total (expect ~82-83%)
uv run pytest tests/test_web_smoke.py -q --ignore=adws  # 1 passed, ~8 s, on a machine with node + chromium
uv run ruff check clreview tests                      # clean
node --check tests/web_smoke.mjs                      # syntax
```

Per-function proof that the zero-coverage targets moved — run after the suite:

```bash
uv run pytest -q --ignore=adws --cov-report=json:/tmp/cov.json >/dev/null
python3 - <<'PY'
import ast, json
d = json.load(open("/tmp/cov.json"))
targets = {"clreview/review.py": ["confirm_match", "reassign"],
           "clreview/service.py": ["overview", "tools"],
           "clreview/sync.py": ["list_jobs"],
           "clreview/connectors/fossology.py": ["_upload_inventory", "_item_map", "_walk",
                                                "_aggregate_copyrights", "_generic_text", "_upload_name"]}
for f, funcs in targets.items():
    ex, mi = set(d["files"][f]["executed_lines"]), set(d["files"][f]["missing_lines"])
    for n in ast.walk(ast.parse(open(f).read())):
        if isinstance(n, ast.FunctionDef) and n.name in funcs:
            lines = set(range(n.lineno, n.end_lineno + 1))
            print(f"{f}:{n.lineno} {n.name}: executed={len(lines & ex)} missing={len(lines & mi)}")
print("TOTAL", round(d["totals"]["percent_covered"], 2), "%")
PY
```

Baseline for comparison is in §0. The smoke test must be run **twice** locally (or three times) to show it is
not flaky: `for i in 1 2 3; do node tests/web_smoke.mjs; done`.

The browser smoke must also be verified against a **fresh** database — move `clreview.db` aside first, or rely
on the `--db` flag added in §4a:

```bash
mv clreview.db /tmp/clreview.db.bak && uv run pytest tests/test_web_smoke.py -q --ignore=adws; mv /tmp/clreview.db.bak clreview.db
```

---

## 7. Out of scope (do not touch)

- C5 (the ~100 unexercised `raise` sites), C7, C8, and every A/B/D/E/F finding.
- Extracting frontend helpers for `node --test` — the scout lists that as longer term; the smoke test stays
  end-to-end over CDP.
- Any frontend refactor, and any change to `clreview/web/*.js` — the polls live in the test, not the app.
  (If a wait seems impossible without app changes, the fetch counter plus a condition is the answer, not a
  `data-ready` attribute.)
- Raising `--cov-fail-under` (stays 75). The new coverage is a fact to report, not a gate to move.
- New test dependencies: no `pytest-mock`, no Playwright, no `requests`. `unittest.mock`, `http.client` and the
  existing CDP-over-WebSocket script are enough.
- Do not delete or weaken any existing assertion in `web_smoke.mjs`; the check count goes from 28 `ok()` call
  sites to 29 (the setup assertion), never down.
