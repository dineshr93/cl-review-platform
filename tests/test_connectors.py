"""Connector tests: retries, error handling, credential hygiene, grouping, stubs."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from clreview.connectors.base import ConnectorError, credential, create_connector
from clreview.connectors.blackduck import BlackDuckConnector, _link
from clreview.connectors.fossology import FossologyConnector
from clreview.connectors.http import HttpError, UnsupportedOperation, get_json, request
from clreview.connectors.scancode import ScanCodeConnector
from clreview.service import sanitize_config

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

    def test_blackduck_capabilities_are_honest(self):
        caps = BlackDuckConnector.capabilities
        self.assertFalse(caps.write_copyrights)
        self.assertFalse(caps.write_license_text)
        self.assertTrue(caps.write_license_names)
        self.assertTrue(caps.verify_writes)
        self.assertIn("copyrights-refresh", caps.notes)


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

    def test_fossology_write_operations_are_unsupported(self):
        conn = self.conn()
        for call in (lambda: conn.update_licenses("k", []), lambda: conn.update_copyrights("k", []),
                     lambda: conn.update_license_text("k", "t")):
            with self.assertRaises(UnsupportedOperation):
                call()


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
