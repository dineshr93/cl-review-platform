"""Connector contract: write retries, honest pagination, SPDX parsing, error envelope."""
from __future__ import annotations

import contextlib
import json
import os
import unittest
from unittest import mock

from clreview.connectors import blackduck as bd_mod
from clreview.connectors import fossology as foss_mod
from clreview.connectors import http as http_mod
from clreview.connectors.base import ConnectorError
from clreview.connectors.blackduck import BlackDuckConnector
from clreview.connectors.fossology import FossologyConnector
from clreview.connectors.http import PaginationLimit, paginated
from clreview.connectors.scancode import ScanCodeConnector, split_license_expression

from helpers import tmpdir


def _over(transport, fn):
    def call(*args, **kw):
        kw["transport"] = transport
        return fn(*args, **kw)

    return call


def real_http(module, transport):
    """Run a connector module's http helpers for real, over a fake transport."""
    stack = contextlib.ExitStack()
    for name in ("get_json", "get_text", "request", "put_json"):
        if hasattr(module, name):
            stack.enter_context(mock.patch.object(module, name, _over(transport, getattr(http_mod, name))))
    return stack


def blackduck(**cfg):
    return BlackDuckConnector("https://bd.example", {"token": "t", **cfg}, {})


def fossology(**cfg):
    return FossologyConnector("https://fossy.example", {"token": "t", "folders_as_scans": False, **cfg}, {})


def full_page(n: int):
    """A transport that answers every listing with `n` items and never says it is finished."""
    def transport(method, url, headers, body, timeout):
        return 200, {"x-total-pages": "999999"}, json.dumps([{"id": i} for i in range(n)]).encode()

    return transport


class WriteRetryTests(unittest.TestCase):
    """A5: a write the server applied but answered 5xx with must not be replayed."""

    def conn(self):
        return blackduck(retries=2)

    def test_post_is_not_retried(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(method)
            return 503, {}, b'{"error":"unavailable"}'

        with real_http(bd_mod, transport):
            with self.assertRaises(ConnectorError):
                self.conn()._send("POST", "https://bd.example/api/copyrights", {"copyright": "x"}, "application/json")
        self.assertEqual(calls, ["POST"])

    def test_put_is_retried(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(method)
            return (503 if len(calls) == 1 else 200), {}, b'{"ok": true}'

        with real_http(bd_mod, transport):
            out = self.conn()._send("PUT", "https://bd.example/api/text", {"text": "x"}, "application/json")
        self.assertEqual(calls, ["PUT", "PUT"])
        self.assertEqual(out["status"], 200)


class PaginationHonestyTests(unittest.TestCase):
    """A6: a page cap is a loud failure, not a short inventory."""

    def test_default_cap_is_pinned(self):
        self.assertEqual(http_mod.MAX_PAGES, 200)

    def test_short_page_ends_the_walk(self):
        self.assertEqual(len(paginated(lambda n: ([{"i": i} for i in range(3)], None), limit=5)), 3)

    def test_reported_total_ends_the_walk(self):
        def page(n):
            return [{"i": i} for i in range(n * 5, n * 5 + 5)], 10

        self.assertEqual(len(paginated(page, limit=5)), 10)

    def test_endless_listing_raises(self):
        with self.assertRaises(PaginationLimit) as ctx:
            paginated(lambda n: ([{"i": i} for i in range(5)], None), limit=5, max_pages=4)
        self.assertIn("refusing to report a partial inventory", str(ctx.exception))

    def test_blackduck_refuses_a_partial_listing(self):
        with real_http(bd_mod, full_page(100)):
            with self.assertRaises(ConnectorError) as ctx:
                blackduck().list_projects()
        self.assertIn("refusing", str(ctx.exception))

    def test_fossology_refuses_a_partial_listing(self):
        with real_http(foss_mod, full_page(100)):
            with self.assertRaises(ConnectorError) as ctx:
                fossology().list_scans()
        self.assertIn("refusing", str(ctx.exception))

    def test_non_numeric_total_pages_is_reported(self):
        def transport(method, url, headers, body, timeout):
            return 200, {"x-total-pages": "many"}, json.dumps([{"id": i} for i in range(100)]).encode()

        with real_http(foss_mod, transport):
            with self.assertRaises(ConnectorError) as ctx:
                fossology()._pages("https://fossy.example/uploads")
        self.assertIn("X-Total-Pages", str(ctx.exception))


class LicenseResolutionTests(unittest.TestCase):
    """A8: the exact license hit can sit past the first hundred results."""

    def test_exact_match_beyond_the_first_hundred_hits(self):
        def transport(method, url, headers, body, timeout):
            offset = int(url.split("offset=")[1].split("&")[0])
            if offset == 0:
                items = [{"name": f"zlib-ish-{i}", "_meta": {"href": f"/api/licenses/{i}"}} for i in range(100)]
                return 200, {}, json.dumps({"items": items, "totalCount": 101}).encode()
            items = [{"name": "zlib", "_meta": {"href": "/api/licenses/real-zlib"}}]
            return 200, {}, json.dumps({"items": items, "totalCount": 101}).encode()

        with real_http(bd_mod, transport):
            self.assertEqual(blackduck()._resolve_license("zlib"), "/api/licenses/real-zlib")

    def test_unknown_license_is_still_reported(self):
        def transport(method, url, headers, body, timeout):
            return 200, {}, json.dumps({"items": [{"name": "other"}], "totalCount": 1}).encode()

        with real_http(bd_mod, transport):
            with self.assertRaises(ConnectorError) as ctx:
                blackduck()._resolve_license("zlib")
        self.assertIn("not found in the Black Duck knowledge base", str(ctx.exception))


class ConnectorErrorContractTests(unittest.TestCase):
    """A9: nothing but ConnectorError leaves a connector."""

    def test_blackduck_html_body_is_a_connector_error(self):
        def transport(method, url, headers, body, timeout):
            return 200, {"content-type": "text/html"}, b"<html><body>proxy</body></html>"

        with real_http(bd_mod, transport):
            with self.assertRaises(ConnectorError) as ctx:
                blackduck().test_connection()
        self.assertIn("not JSON", str(ctx.exception))

    def test_blackduck_empty_body_is_not_an_attribute_error(self):
        def transport(method, url, headers, body, timeout):
            return 204, {}, b""

        with real_http(bd_mod, transport):
            self.assertEqual(blackduck().test_connection(),
                             {"ok": True, "tool": "blackduck", "total_projects": 0})

    def test_null_license_type_falls_back_to_declared(self):
        def transport(method, url, headers, body, timeout):
            return 200, {}, json.dumps({"name": "Zlib", "spdxId": "Zlib"}).encode()

        with real_http(bd_mod, transport):
            licenses, _ = blackduck()._licenses([{"license": "/api/licenses/1", "licenseType": None}])
        self.assertEqual([l.license_type for l in licenses], ["declared"])

    def test_fossology_html_body_is_a_connector_error(self):
        def transport(method, url, headers, body, timeout):
            return 200, {"content-type": "text/html"}, b"<html><body>login</body></html>"

        with real_http(foss_mod, transport):
            with self.assertRaises(ConnectorError) as ctx:
                fossology().test_connection()
        self.assertIn("not JSON", str(ctx.exception))


class LicenseExpressionTests(unittest.TestCase):
    """A7: one entry per operand of an SPDX expression, no invented licenses."""

    def test_with_binds_an_exception_and_stays_one_license(self):
        self.assertEqual(split_license_expression("Apache-2.0 WITH LLVM-exception"),
                         ["Apache-2.0 WITH LLVM-exception"])

    def test_and_and_or_split_operands(self):
        self.assertEqual(split_license_expression("MIT AND Apache-2.0 OR BSD-3-Clause"),
                         ["MIT", "Apache-2.0", "BSD-3-Clause"])

    def test_parentheses_only_group(self):
        self.assertEqual(split_license_expression("(MIT OR Apache-2.0) AND BSD-3-Clause"),
                         ["MIT", "Apache-2.0", "BSD-3-Clause"])

    def test_or_inside_an_identifier_is_not_an_operator(self):
        self.assertEqual(split_license_expression("GPL-3.0-or-later OR MIT"), ["GPL-3.0-or-later", "MIT"])
        self.assertEqual(split_license_expression("GPL-2.0-or-later WITH Classpath-exception-2.0"),
                         ["GPL-2.0-or-later WITH Classpath-exception-2.0"])

    def test_lowercase_operators_are_operators(self):
        self.assertEqual(split_license_expression("MIT and Apache-2.0"), ["MIT", "Apache-2.0"])

    def test_single_license_and_duplicates(self):
        self.assertEqual(split_license_expression(" Zlib "), ["Zlib"])
        self.assertEqual(split_license_expression("MIT AND MIT"), ["MIT"])


class ScanCodeExpressionTests(unittest.TestCase):
    """A7 through the connector: the component carries the expression, not its halves."""

    def scancode(self, payload):
        path = os.path.join(tmpdir(), "scan.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return ScanCodeConnector("", {"json_path": path}, {})

    def test_package_expression_is_one_license(self):
        conn = self.scancode({"packages": [{"package_name": "lib", "package_version": "1.0",
                                            "license_expression": "Apache-2.0 WITH LLVM-exception"}]})
        component = conn.fetch_inventory("s")[0]
        self.assertEqual([l.name for l in component.licenses], ["Apache-2.0 WITH LLVM-exception"])
        self.assertEqual([l.spdx for l in component.licenses], ["Apache-2.0 WITH LLVM-exception"])

    def test_file_detection_expression_is_one_license(self):
        conn = self.scancode({"files": [
            {"path": "lib-1.0/LICENSE", "license_detections":
                [{"license_expression": "Apache-2.0 WITH LLVM-exception"}]},
            {"path": "lib-1.0/src.c", "license_detections": [{"license_expression": "MIT AND Apache-2.0"}]},
        ]})
        component = conn.fetch_inventory("s")[0]
        self.assertEqual([l.name for l in component.licenses],
                         ["Apache-2.0 WITH LLVM-exception", "MIT", "Apache-2.0"])


if __name__ == "__main__":
    unittest.main()
