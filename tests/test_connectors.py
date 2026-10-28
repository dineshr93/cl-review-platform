"""Connector tests: retries, error handling, credential hygiene, grouping, stubs."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from clreview.connectors import blackduck
from clreview.connectors.base import ConnectorError, credential, create_connector
from clreview.connectors.blackduck import BlackDuckConnector, _link
from clreview.connectors.fossology import FossologyConnector
from clreview.connectors.http import HttpError, UnsupportedOperation, get_json, request
from clreview.connectors.scancode import ScanCodeConnector
from clreview.db import DB
from clreview.domain import sha256
from clreview.service import Service, sanitize_config

from helpers import tmpdir


class HttpTests(unittest.TestCase):
    def test_transient_status_is_retried(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(status := (500 if len(calls) == 0 else 200))
            return status, {}, b'{"ok": true}'

        status, _, data = request("GET", "http://x/api", transport=transport, retries=2)
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(data), {"ok": True})

    def test_client_error_is_not_retried(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(1)
            return 401, {}, b'{"error":"bad token"}'

        status, _, _ = request("GET", "http://x/api", transport=transport, retries=3)
        self.assertEqual(status, 401)
        self.assertEqual(len(calls), 1)

    def test_error_body_is_bounded(self):
        def transport(method, url, headers, body, timeout):
            return 500, {}, b"x" * 5000

        with self.assertRaises(HttpError) as ctx:
            get_json("http://x/api", transport=transport, retries=0)
        self.assertLessEqual(len(ctx.exception.body), 300)
        self.assertEqual(ctx.exception.status, 500)


class CredentialTests(unittest.TestCase):
    def test_credential_reads_named_env_var(self):
        with mock.patch.dict(os.environ, {"BD_TEST_TOKEN": "s3cret"}):
            self.assertEqual(credential({"token": "inline"}, {"token": "BD_TEST_TOKEN"}, "token"), "s3cret")
            self.assertEqual(credential({"token": "inline"}, {}, "token"), "inline")

    def test_config_sanitization_masks_secrets(self):
        out = sanitize_config({"token": "abc", "username": "u", "password": "p", "timeout": 3})
        self.assertEqual(out["token"], "***")
        self.assertEqual(out["username"], "***")
        self.assertEqual(out["password"], "***")
        self.assertEqual(out["timeout"], 3)

    def test_missing_token_fails_without_leaking(self):
        conn = BlackDuckConnector("https://bd.example", {"timeout": 1}, {})
        with self.assertRaises(ConnectorError) as ctx:
            conn.list_projects()
        self.assertNotIn("Bearer", str(ctx.exception))

    def test_literal_credential_never_reaches_persisted_state(self):
        db = DB(":memory:")
        svc = Service(db)
        with self.assertRaises(ValueError) as ctx:
            svc.add_connection("pl", "mock-pl", "leaky", "", {"token": "SUPER-SECRET"}, {})
        self.assertIn("auth_env", str(ctx.exception))
        rows = db.query("SELECT config_json FROM connections")
        self.assertNotIn("SUPER-SECRET", json.dumps([dict(r) for r in rows]))
        db.close()

    def test_blackduck_capabilities_are_honest(self):
        caps = BlackDuckConnector.capabilities
        self.assertTrue(caps.write_copyrights)
        self.assertTrue(caps.write_license_text)
        self.assertTrue(caps.write_license_names)
        self.assertTrue(caps.verify_writes)
        self.assertIn("per component-version origin", caps.notes)


class BlackDuckWriteTests(unittest.TestCase):
    """Writes proven by apis/blackduck_Blackduck-REST-API-v2026.4.2.md (origin copyrights, BOM license text)."""

    BOM = "https://bd/api/projects/p/versions/v/components/c"
    ORIGIN = "https://bd/api/components/cid/versions/cvid/origins/oid"
    LIC = "https://bd/api/projects/p/versions/v/components/c/licenses/lid"

    def conn(self):
        return BlackDuckConnector("https://bd.example", {"token": "t"}, {})

    def patch_api(self, calls, licenses=None, copyright_texts=("Copyright 2008 KB",)):
        licenses = licenses if licenses is not None else [{"licenseDisplay": "MIT", "license": self.LIC}]

        def get_json(url, **kw):
            path = url.split("?")[0]
            if path.startswith(self.BOM):
                return {"componentName": "lib", "licenses": licenses,
                        "origins": [{"name": "maven:lib", "origin": self.ORIGIN}]}
            if path == "https://bd.example/api/licenses":
                return {"totalCount": 1, "items": [{"name": "MIT", "_meta": {"href": self.LIC}}]}
            if path == self.LIC:
                return {"name": "MIT", "licenseDisplay": "MIT"}
            if path == self.ORIGIN + "/copyrights":
                return {"totalCount": len(copyright_texts),
                        "items": [{"updatedCopyright": t, "active": True} for t in copyright_texts]}
            if path.startswith(self.ORIGIN):
                return {"originName": "maven:lib"}
            raise AssertionError(f"unexpected GET {url}")

        def get_text(url, **kw):
            if url == self.LIC + "/text":
                return "Permission is hereby granted."
            raise AssertionError(f"unexpected GET {url}")

        def request(method, url, headers=None, body=None, **kw):
            payload = json.loads(body) if isinstance(body, (str, bytes)) else body
            calls.append((method, url, dict(headers or {}), payload))
            return 200, {}, b'{"ok": true}'

        def put_json(url, body, **kw):
            calls.append(("PUT", url, dict(kw.get("headers") or {}), body))
            return 200, {"ok": True}

        return [
            mock.patch.object(blackduck, "get_json", get_json),
            mock.patch.object(blackduck, "get_text", get_text),
            mock.patch.object(blackduck, "request", request),
            mock.patch.object(blackduck, "put_json", put_json),
        ]

    def with_api(self, fn, **kw):
        calls: list = []
        patches = self.patch_api(calls, **kw)
        for p in patches:
            p.start()
        try:
            return fn(calls)
        finally:
            for p in patches:
                p.stop()

    def test_read_component_reports_copyrights_and_bom_license_text(self):
        out = self.with_api(lambda calls: self.conn().read_component(self.BOM))
        self.assertEqual(out["copyrights"], ["Copyright 2008 KB"])
        self.assertEqual(out["licenses"], ["MIT"])
        self.assertEqual(out["license_texts"][0]["content_hash"], sha256("Permission is hereby granted."))

    def test_copyright_write_posts_only_missing_statements(self):
        result = self.with_api(lambda calls: self.conn().update_copyrights(
            self.BOM, ["Copyright 2008 KB", "Copyright 2024 Reviewer"]))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"]["created"], ["Copyright 2024 Reviewer"])

    def test_copyright_write_sends_copyright_media_and_body(self):
        def run(calls):
            self.conn().update_copyrights(self.BOM, ["Copyright 2024 Reviewer"])
            return calls[-1]
        method, url, headers, body = self.with_api(run)
        self.assertEqual((method, url), ("POST", self.ORIGIN + "/copyrights"))
        self.assertEqual(headers["Accept"], "application/vnd.blackducksoftware.copyright-4+json")
        self.assertEqual(headers["Content-Type"], "application/vnd.blackducksoftware.copyright-4+json")
        self.assertEqual(body, {"copyright": "Copyright 2024 Reviewer"})

    def test_license_text_write_puts_bom_component_license_text(self):
        def run(calls):
            result = self.conn().update_license_text(self.BOM, "New text", "LICENSE")
            return result, calls[-1]
        result, (method, url, headers, body) = self.with_api(run)
        self.assertEqual((method, url), ("PUT", self.LIC + "/text"))
        self.assertEqual(headers["Accept"], "application/vnd.blackducksoftware.bill-of-materials-7+json")
        self.assertEqual(body, {"text": "New text"})
        self.assertEqual(result["status"], 200)

    def test_license_name_write_puts_and_returns_without_raising(self):
        def run(calls):
            result = self.conn().update_licenses(self.BOM, [{"name": "MIT"}])
            return result, calls[-1]
        result, (method, url, _headers, body) = self.with_api(run)
        self.assertEqual((method, url), ("PUT", self.BOM))
        self.assertEqual(body["licenses"], [{"licenseDisplay": "MIT", "license": self.LIC}])
        self.assertEqual(result["status"], 200)

    def test_ambiguous_license_text_target_is_refused_not_guessed(self):
        two = [{"licenseDisplay": "MIT", "license": self.LIC},
               {"licenseDisplay": "Apache-2.0", "license": self.LIC + "2"}]
        with self.assertRaises(ConnectorError) as ctx:
            self.with_api(lambda calls: self.conn().update_license_text(self.BOM, "t", "LICENSE"),
                          licenses=two)
        self.assertIn("cannot decide", str(ctx.exception))

    def test_license_text_resolved_by_file_name_when_multiple(self):
        two = [{"licenseDisplay": "MIT", "license": self.LIC},
               {"licenseDisplay": "Apache-2.0", "license": self.LIC + "2"}]

        def run(calls):
            self.conn().update_license_text(self.BOM, "t", "LICENSE-apache-2-0.txt")
            return calls[-1][:2]
        self.assertEqual(self.with_api(run, licenses=two), ("PUT", self.LIC + "2/text"))


class BlackDuckSharedOriginTests(unittest.TestCase):
    """B6: a BOM repeats the same origin on every component; the reads must not repeat with it."""

    BASE = "https://bd.example"
    BOM_LIST = BASE + "/api/projects/p/versions/v/components"
    COMP = BASE + "/api/components/c1"
    ORIGIN = BASE + "/api/components/cid/versions/cvid/origins/oid"

    def setUp(self):
        self.calls: list[str] = []
        self.routes = {
            self.BOM_LIST: {"totalCount": 6, "items": [
                {"componentName": f"lib{i}", "componentVersionName": "1.0", "origins": [self.ORIGIN],
                 "licenses": [], "_meta": {"href": f"{self.BASE}/api/components/c{i}"}} for i in range(6)]},
            self.COMP: {"componentName": "lib1", "origins": [self.ORIGIN], "licenses": []},
            self.ORIGIN: {"originName": "maven:lib", "_meta": {"href": self.ORIGIN, "links": [
                {"rel": "component-origin-copyrights", "href": self.ORIGIN + "/copyrights"}]}},
            self.ORIGIN + "/copyrights": {"totalCount": 1,
                                          "items": [{"updatedCopyright": "Copyright 2008 KB", "active": True}]},
        }

        def get_json(url, **kw):
            path = url.split("?")[0]
            self.calls.append(path)
            if path not in self.routes:
                raise AssertionError(f"unexpected GET {url}")
            return self.routes[path]

        def request(method, url, headers=None, body=None, **kw):
            self.calls.append(f"{method} {url.split('?')[0]}")
            return 200, {}, b'{"ok": true}'

        self.patches = [mock.patch.object(blackduck, "get_json", get_json),
                        mock.patch.object(blackduck, "request", request)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def conn(self):
        return BlackDuckConnector(self.BASE, {"token": "t"}, {})

    def test_shared_origin_is_read_once_for_the_whole_bom(self):
        out = self.conn().fetch_inventory("p", "v")
        self.assertEqual(len(out), 6)
        self.assertTrue(all(c.copyrights for c in out))          # nobody lost their evidence
        self.assertEqual([c.text for c in out[0].copyrights], ["Copyright 2008 KB"])
        self.assertEqual(self.calls.count(self.ORIGIN), 1)       # was once per component
        self.assertEqual(self.calls.count(self.ORIGIN + "/copyrights"), 1)
        self.assertEqual(len(self.calls), 3)                     # listing + origin + collection

    def test_a_second_inventory_read_asks_the_network_again(self):
        self.conn().fetch_inventory("p", "v")
        before = len(self.calls)
        self.conn().fetch_inventory("p", "v")
        self.assertEqual(len(self.calls) - before, 3)            # the memo died with the call

    def test_each_new_notice_is_one_post_and_the_collection_is_read_once(self):
        result = self.conn().update_copyrights(self.COMP, ["Copyright 2008 KB", "Copyright 2024 Reviewer"])
        self.assertEqual(result["body"]["created"], ["Copyright 2024 Reviewer"])
        self.assertEqual([c for c in self.calls if c.startswith("POST")],
                         [f"POST {self.ORIGIN}/copyrights"])      # one POST per genuinely new notice
        self.assertEqual(self.calls.count(self.ORIGIN), 1)        # the origin used to be read twice
        self.assertEqual(self.calls.count(self.ORIGIN + "/copyrights"), 1)
        self.assertEqual(len(self.calls), 4)                      # component, origin, collection, POST


class BlackDuckLinkTests(unittest.TestCase):
    def test_link_from_meta_links(self):
        obj = {"_meta": {"links": [{"rel": "text", "href": "https://bd/license/text"}]}}
        self.assertEqual(_link(obj, "text"), "https://bd/license/text")

    def test_link_from_plain_field(self):
        self.assertEqual(_link({"text": "https://bd/text"}, "text"), "https://bd/text")
        self.assertEqual(_link({"text": {"_meta": {"href": "https://bd/href"}}}, "text"), "https://bd/href")


class FossologyGroupingTests(unittest.TestCase):
    def conn(self):
        return FossologyConnector("https://fossy.example", {"agents": "nomos"}, {"token": "t"})

    def files(self, paths):
        return [{"filePath": p, "findings": {"scanner": ["MIT"], "copyright": ["Copyright 2020 X"]}} for p in paths]

    def test_multiple_top_level_dirs_become_components(self):
        groups = self.conn()._group(self.files(["pkg-a-1.0/LICENSE", "pkg-b-2.0/LICENSE"]), "upload")
        self.assertEqual(sorted(groups), ["pkg-a-1.0", "pkg-b-2.0"])
        self.assertEqual(groups["pkg-a-1.0"]["licenses"]["pkg-a-1.0/LICENSE"], ["MIT"])

    def test_single_package_tree_is_one_component(self):
        groups = self.conn()._group(self.files(["pkg-a-1.0/src/x.c", "pkg-a-1.0/LICENSE"]), "upload")
        self.assertEqual(list(groups), ["pkg-a-1.0"])

    def test_root_files_belong_to_upload_component(self):
        groups = self.conn()._group(self.files(["LICENSE", "COPYING"]), "upload-7")
        self.assertEqual(list(groups), ["upload-7"])

    def test_one_dir_plus_root_files_groups_deterministically(self):
        conn = self.conn()
        a = conn._group(self.files(["pkg-a-1.0/LICENSE", "README"]), "upload")
        b = conn._group(self.files(["README", "pkg-a-1.0/LICENSE"]), "upload")
        self.assertEqual((list(a), list(b)), (["pkg-a-1.0"], ["pkg-a-1.0"]))
        self.assertEqual(sorted(a["pkg-a-1.0"]["paths"]), ["README", "pkg-a-1.0/LICENSE"])
        self.assertEqual(sorted(b["pkg-a-1.0"]["paths"]), ["README", "pkg-a-1.0/LICENSE"])

    def test_fossology_write_operations_are_unsupported(self):
        conn = self.conn()
        for call in (lambda: conn.update_licenses("k", []), lambda: conn.update_copyrights("k", []),
                     lambda: conn.update_license_text("k", "t")):
            with self.assertRaises(UnsupportedOperation):
                call()


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
        "/uploads/7/copyrights": [
            {"copyright": FOSS_AGGREGATE, "filePath": ["zlib-1.2.11/zutil.c"]},
            # already reported per file, a path outside this component, and an entry with no text
            {"copyright": "Copyright (C) 1995-2017 Jean-loup Gailly", "filePath": ["zlib-1.2.11/zutil.c"]},
            {"copyright": "Copyright (C) 2011 String Path", "filePath": "other/README"},
            {"filePath": ["zlib-1.2.11/zutil.c"]},
        ],
        "/uploads/7/topitem": {"message": 1},
        "/uploads/7/item/1/tree/view": [{"fileDetails": {"fileName": "zlib-1.2.11", "id": 2, "isContainer": True}}],
        "/uploads/7/item/2/tree/view": [
            {"fileDetails": {"fileName": "LICENSE", "id": 4}},
            {"fileDetails": {"fileName": "zutil.c", "id": 5}},
            {"fileDetails": {"fileName": "no-id"}},
        ],
        "/license/Zlib": {"text": "The zlib License text"},
    }


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
        conn._dir_listings["7"] = {"": []}          # the tree is known and the file is not in it
        c = conn._upload_inventory("7")[0]
        self.assertEqual([t.kind for t in c.license_texts], ["generic_metadata"])

    def test_walk_stops_at_the_depth_cap(self):
        conn = FakeFossology(foss_routes(), {})
        for i in range(60):
            conn.routes[f"/uploads/7/item/{100 + i}/tree/view"] = [
                {"fileDetails": {"fileName": f"d{i}", "id": 101 + i, "isContainer": True}}]
        conn.routes["/uploads/7/topitem"] = {"message": 100}
        conn._item_map("7")
        self.assertLessEqual(len(conn._dir_listings["7"]), 41)     # the cap held, all 60 did not

    def test_walk_only_opens_the_branches_that_hold_what_it_asked_for(self):
        routes = foss_routes()
        routes["/uploads/7/item/1/tree/view"] = [
            {"fileDetails": {"fileName": "zlib-1.2.11", "id": 2, "isContainer": True}},
            {"fileDetails": {"fileName": "far", "id": 40, "isContainer": True}},
        ]
        routes["/uploads/7/item/40/tree/view"] = [
            {"fileDetails": {"fileName": "LICENSE", "id": 41}}]
        conn = FakeFossology(routes, {"/uploads/7/item/4/view": FOSS_LICENSE_TEXT})
        out = conn._item_map("7", ["zlib-1.2.11/LICENSE"])
        self.assertEqual(out["zlib-1.2.11/LICENSE"], 4)
        self.assertNotIn("/uploads/7/item/40/tree/view", conn.calls)   # the far branch untouched
        self.assertEqual(conn.calls.count("/uploads/7/topitem"), 1)

    def test_a_partial_walk_is_not_mistaken_for_the_whole_tree(self):
        conn = FakeFossology(foss_routes(), {"/uploads/7/item/4/view": FOSS_LICENSE_TEXT})
        conn._item_map("7", ["zlib-1.2.11/LICENSE"])          # stopped early: only 2 dirs read
        before = list(conn.calls)
        out = conn._item_map("7", ["zlib-1.2.11/zutil.c", "zlib-1.2.11/LICENSE"])
        self.assertEqual(out["zlib-1.2.11/zutil.c"], 5)        # found, not reported as missing
        self.assertEqual(conn.calls, before)                   # the branch was already listed

    def test_topitem_may_answer_with_a_list_and_an_unreadable_tree_is_empty(self):
        routes = foss_routes()
        routes["/uploads/7/topitem"] = [{"message": 1}]
        del routes["/uploads/7/item/1/tree/view"]
        conn = FakeFossology(routes, {"/uploads/7/item/4/view": FOSS_LICENSE_TEXT})
        self.assertEqual(conn._item_map("7"), {})                      # _walk swallowed the 404
        c = conn._upload_inventory("7")[0]
        self.assertEqual([t.kind for t in c.license_texts], ["generic_metadata"])


class ScanCodeTests(unittest.TestCase):
    def test_reads_packages_and_files(self):
        d = tmpdir()
        path = os.path.join(d, "scancode.json")
        data = {
            "packages": [{"package_name": "zlib", "package_version": "1.2.11", "purl": "pkg:github/madler/zlib@1.2.11",
                          "license_expression": "zlib"}],
            "files": [
                {"path": "pkg/LICENSE", "license_detections": [{"license_expression": "mit"}],
                 "copyrights": [{"value": "Copyright (c) 2020 X"}], "text": "MIT text"},
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        conn = ScanCodeConnector("", {"json_path": path}, {})
        inv = conn.fetch_inventory("scan")
        self.assertEqual(inv[0].name, "zlib")
        self.assertEqual(inv[0].purl, "pkg:github/madler/zlib@1.2.11")
        conn._data = None
        inv2 = conn.fetch_inventory("scan")
        self.assertEqual(len(inv2), 1)  # packages take precedence

    def test_grouped_files_carry_license_text_only_when_present(self):
        d = tmpdir()
        path = os.path.join(d, "scancode.json")
        data = {"files": [
            {"path": "a/LICENSE", "license_detections": [{"license_expression": "mit"}],
             "copyrights": [{"value": "Copyright 2020 A"}], "text": "license body"},
            {"path": "a/src/x.c", "license_detections": [{"license_expression": "mit"}]},
        ]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        conn = ScanCodeConnector("", {"json_path": path}, {})
        inv = conn.fetch_inventory("scan")
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0].license_texts[0].kind, "source_file")
        self.assertEqual(inv[0].license_texts[0].file_path, "a/LICENSE")

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(ConnectorError):
            ScanCodeConnector("", {"json_path": "/nonexistent.json"}, {}).test_connection()


class StubTests(unittest.TestCase):
    def test_fossid_is_registered_but_not_faked(self):
        conn = create_connector("fossid", "pl", "https://fossid.example", {}, {})
        with self.assertRaises(ConnectorError) as ctx:
            conn.list_projects()
        self.assertTrue(ctx.exception.unsupported)
        self.assertFalse(conn.capabilities.read_components)

    def test_role_validation(self):
        with self.assertRaises(ConnectorError):
            create_connector("blackduck", "sl", "https://bd", {}, {})
        with self.assertRaises(ConnectorError):
            create_connector("nope", "pl", "", {}, {})


if __name__ == "__main__":
    unittest.main()
