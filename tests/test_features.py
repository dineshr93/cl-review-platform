"""Tests for the added review features: config profiles, license map, false positives,
delta comparison, undo, HTML export, health capabilities, and per-component apply."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from clreview.config import config_profiles, load_config, profile_connection
from clreview.folder_mapping import resolve_folder, sanitize_folder_name
from clreview.undo import UndoService

from helpers import (GENERIC_ZLIB, make_review, make_service, paths_for,
                     pl_component, rewrite_fixtures, tmpdir)
from test_sync import approve_all, components
from test_export_readonly import Server


ROOT = Path(__file__).resolve().parent.parent


class ConfigAndFolderTests(unittest.TestCase):
    def test_sample_config_loads_profiles_and_secret_names(self):
        config = load_config(str(ROOT / "config.yaml"))
        profiles = config_profiles(config)
        self.assertEqual(set(profiles), {"local", "production"})
        bd = profile_connection(profiles["production"], "bd")
        self.assertEqual(bd["tool"], "blackduck")
        self.assertEqual(bd["base_url"], "https://blackduck.example")
        self.assertEqual(bd["auth_env"], {"token": "BLACKDUCK_TOKEN"})
        self.assertTrue(bd["config"]["verify_tls"])

    def test_folder_mapping_sanitizes_and_honours_override(self):
        self.assertEqual(sanitize_folder_name("demo/app:1.0"), "demo_app_1.0")
        mapping = {
            "template": "{bd_project}/{bd_version}",
            "sanitize": True,
            "overrides": {"demo-app/1.0.0": "demo_app_1_0_0"},
            "regex": [{"pattern": "[^A-Za-z0-9]", "replacement": "_"}],
        }
        self.assertEqual(resolve_folder(mapping, "demo-app", "1.0.0"), "demo_app_1_0_0")
        mapping_no_override = {"template": "{bd_project}/{bd_version}", "sanitize": False,
                               "regex": [{"pattern": "[^A-Za-z0-9]", "replacement": "_"}]}
        self.assertEqual(resolve_folder(mapping_no_override, "demo-app", "1.0.0"), "demo_app_1_0_0")


class LicenseMapTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        pl = [pl_component(name="zlib", version="1.2.11",
                           licenses=[{"name": "Zlib", "spdx": "Zlib"}])]
        sl = [{"external_id": "zlib-1.2.11", "name": "zlib", "version": "1.2.11",
               "licenses": [{"name": "MIT", "spdx": "MIT", "evidence_paths": ["zlib/LICENSE"]}]}]
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]

    def test_upsert_delete_and_lookup(self):
        row = self.svc.upsert_license_map("MIT", "MIT", "MIT", "alice")
        self.assertEqual(row["source"], "manual")
        lookup = self.svc.license_map_lookup()
        self.assertEqual(lookup["MIT"]["bd_license"], "MIT")
        self.svc.delete_license_map(row["id"], "alice")
        self.assertFalse(any(r["short_name"] == "MIT" for r in self.svc.license_map_rows()))

    def test_approval_uses_the_mapped_bd_license(self):
        self.svc.upsert_license_map("GPL", "GPL-2.0-only", "GPL-2.0-only", "alice")
        self.dir2 = tmpdir()
        pl = [pl_component(name="foo", version="1.0",
                           licenses=[{"name": "Zlib", "spdx": "Zlib"}])]
        sl = [{"external_id": "foo-1.0", "name": "foo", "version": "1.0",
               "licenses": [{"name": "GPL", "spdx": "GPL-2.0-only", "evidence_paths": ["foo/LICENSE"]}]}]
        svc, _db, mid = make_service(self.dir2, pl, sl)
        svc.upsert_license_map("GPL", "GPL-2.0-only", "GPL-2.0-only", "alice")
        review, _sync = make_review(svc)
        match_id = review.list_components(mid)["items"][0]["id"]
        row = [r for r in review.detail(match_id)["comparison"]["licenses"] if r["kind"] == "sl_only"][0]
        self.assertEqual(row["key"], "GPL-2.0-ONLY")
        out = review.decide(match_id, "license_name", row["key"], "approve_sl", reviewer="alice")
        decision = out["decision"]
        self.assertEqual(decision["approved_value"], "GPL-2.0-only")


class FalsePositiveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        pl = [pl_component(name="zlib", version="1.2.11",
                           licenses=[{"name": "Zlib", "spdx": "Zlib"}])]
        sl = [{"external_id": "zlib-1.2.11", "name": "zlib", "version": "1.2.11",
               "licenses": [{"name": "Zlib", "spdx": "Zlib"},
                            {"name": "MIT", "spdx": "MIT", "evidence_paths": ["zlib/LICENSE"]}]}]
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]

    def test_component_scope_rule_is_auto_applied_and_revocable(self):
        detail = self.review.detail(self.match_id)
        row = [r for r in detail["comparison"]["licenses"] if r["kind"] == "sl_only"][0]
        self.assertEqual(row["key"], "MIT")
        self.review.decide(self.match_id, "license_name", row["key"], "mark_false_positive",
                           reviewer="alice", scope="component", path_pattern="zlib/LICENSE")
        detail = self.review.detail(self.match_id)
        row = [r for r in detail["comparison"]["licenses"] if r["key"] == "MIT"][0]
        self.assertTrue(row["auto_false_positive"])
        rules = self.review.list_false_positive_rules()
        self.assertEqual(rules[-1]["scope"], "component")
        self.assertEqual(rules[-1]["match_id"], self.match_id)
        self.review.revoke_false_positive_rule(rules[-1]["id"], "alice")
        row = [r for r in self.review.detail(self.match_id)["comparison"]["licenses"] if r["key"] == "MIT"][0]
        self.assertFalse(row["auto_false_positive"])

    def test_global_rule_applies_to_another_component(self):
        self.dir2 = tmpdir()
        pl = [pl_component(name="a", version="1.0", licenses=[{"name": "Zlib", "spdx": "Zlib"}]),
              pl_component(name="b", version="1.0", licenses=[{"name": "Zlib", "spdx": "Zlib"}])]
        sl = [{"external_id": "a-1.0", "name": "a", "version": "1.0",
               "licenses": [{"name": "Zlib", "spdx": "Zlib"},
                            {"name": "MIT", "spdx": "MIT", "evidence_paths": ["a/LICENSE"]}]},
              {"external_id": "b-1.0", "name": "b", "version": "1.0",
               "licenses": [{"name": "Zlib", "spdx": "Zlib"},
                            {"name": "MIT", "spdx": "MIT", "evidence_paths": ["b/LICENSE"]}]}]
        svc, _db, mid = make_service(self.dir2, pl, sl)
        review, _sync = make_review(svc)
        items = review.list_components(mid)["items"]
        a_id = next(i["id"] for i in items if i["pl_name"] == "a")
        b_id = next(i["id"] for i in items if i["pl_name"] == "b")
        a_row = [r for r in review.detail(a_id)["comparison"]["licenses"] if r["key"] == "MIT"][0]
        review.decide(a_id, "license_name", a_row["key"], "mark_false_positive",
                      reviewer="alice", scope="global", path_pattern="*")
        b_row = [r for r in review.detail(b_id)["comparison"]["licenses"] if r["key"] == "MIT"][0]
        self.assertTrue(b_row["auto_false_positive"])
        rules = review.list_false_positive_rules()
        review.revoke_false_positive_rule(rules[-1]["id"], "alice")
        b_row = [r for r in review.detail(b_id)["comparison"]["licenses"] if r["key"] == "MIT"][0]
        self.assertFalse(b_row["auto_false_positive"])


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        pl = [pl_component(name="alpha", version="1.0",
                           licenses=[{"name": "Zlib", "spdx": "Zlib"}])]
        sl = [{"external_id": "alpha-1.0", "name": "alpha", "version": "1.0",
               "licenses": [{"name": "MIT", "spdx": "MIT", "evidence_paths": ["alpha/LICENSE"]}]}]
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        row = [r for r in self.review.detail(self.match_id)["comparison"]["licenses"] if r["kind"] == "sl_only"][0]
        self.review.decide(self.match_id, "license_name", row["key"], "approve_sl", reviewer="alice")

    def test_delta_compares_previous_version_and_carries_decisions(self):
        self.db.execute("UPDATE mappings SET created_at=? WHERE id=?",
                        ("2024-01-01T00:00:00+00:00", self.mid))
        paths = paths_for(self.svc, self.mid)
        pl2 = [pl_component(name="alpha", version="1.0",
                            licenses=[{"name": "Zlib", "spdx": "Zlib"}])]
        sl2 = [{"external_id": "alpha-1.0", "name": "alpha", "version": "1.0",
                "licenses": [{"name": "MIT", "spdx": "MIT", "evidence_paths": ["alpha/LICENSE"]},
                             {"name": "Apache-2.0", "spdx": "Apache-2.0", "evidence_paths": ["alpha/LICENSE"]}]}]
        rewrite_fixtures(paths, pl_components=pl2, sl_components=sl2)
        with open(paths["pl"], encoding="utf-8") as fh:
            fixture = json.load(fh)
        fixture["projects"][0]["versions"][0]["id"] = "2.0"
        fixture["projects"][0]["versions"][0]["name"] = "2.0"
        with open(paths["pl"], "w", encoding="utf-8") as fh:
            json.dump(fixture, fh)
        current = self.svc.mapping(self.mid)
        mid2 = self.svc.create_mapping("v2", current["pl_connection_id"], "proj", "2.0",
                                       current["sl_connection_id"], "scan", "test")["id"]
        self.svc.refresh(mid2, "alice")

        delta = self.review.delta(mid2, apply_decisions=True)
        self.assertEqual(delta["previous"]["version"], "1.0")
        self.assertEqual(delta["current"]["version"], "2.0")
        self.assertEqual([c["name"] for c in delta["changed_components"]], ["alpha"])
        self.assertEqual(delta["new_findings"][0]["new_licenses"], ["APACHE-2.0"])
        self.assertEqual(len(delta["carried_decisions"]), 1)

        current_id = self.review.list_components(mid2)["items"][0]["id"]
        decisions = self.db.query("SELECT * FROM decisions WHERE match_id=?", (current_id,))
        self.assertTrue(any(d["field"] == "license_name" and d["entry_key"] == "MIT" for d in decisions))


class UndoTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.svc, self.db, self.mid = make_service(self.dir, *components())
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        approve_all(self.review, self.match_id)
        self.job = self.sync.create_job(self.mid, dry_run=False, confirm=True, actor="alice", finalize=True)
        self.undo = UndoService(self.sync)

    def _pl_state(self):
        return self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"]).read_component(
            "proj|1.0|zlib")

    def test_undo_preview_and_restore(self):
        preview = self.undo.preview(self.job["id"])
        self.assertTrue(all(row["possible"] for row in preview["items"]))
        out = self.undo.undo(self.job["id"], confirm=True, actor="alice")
        self.assertEqual(out["ok"], True)
        self.assertEqual(self.sync.job(self.job["id"])["status"], "undone")
        state = self._pl_state()
        self.assertEqual(state["licenses"], ["Zlib"])
        self.assertEqual(state["copyrights"], ["Copyright (C) 1995-2017 Jean-loup Gailly and Mark Adler"])
        self.assertEqual(state["license_texts"][0]["text"], GENERIC_ZLIB)
        self.assertEqual(state["reviewStatus"], "NOT_REVIEWED")
        sl = self.svc.connector_for(self.svc.mapping(self.mid)["sl_connection_id"])
        self.assertEqual(sl.upload_status("scan"), "Open")


class HtmlExportTests(unittest.TestCase):
    def setUp(self):
        from clreview.db import DB
        from clreview.server import seed_demo
        self.db = DB(":memory:")
        self.mapping_id = seed_demo(self.db)["mapping_id"]
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_html_export_is_a_self_contained_report(self):
        status, ctype, text = self.server.get_raw(f"/api/mappings/{self.mapping_id}/export.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("<table>", text)
        self.assertIn("</html>", text)
        self.assertIn("demo-app", text)


class HealthAndCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        from clreview.db import DB
        from clreview.server import seed_demo
        self.db = DB(":memory:")
        seed_demo(self.db)
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_health_lists_connections_and_capabilities(self):
        status, body = self.server.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["connections"]), 2)
        caps = body["connections"][0]["capabilities"]
        self.assertTrue(caps["write_license_names"])
        self.assertTrue(caps["verify_writes"])
        status, caps = self.server.call("GET", "/api/connections/1/capabilities")
        self.assertEqual(status, 200)
        self.assertIn("write_license_text", caps)


class ApiDeleteTests(unittest.TestCase):
    def setUp(self):
        from clreview.db import DB
        from clreview.server import seed_demo
        self.db = DB(":memory:")
        seed_demo(self.db)
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_delete_license_map_through_http(self):
        status, row = self.server.call("POST", "/api/license-map",
                                       {"short_name": "MIT-Alt", "spdx": "MIT", "bd_license": "MIT"},
                                       reviewer="alice")
        self.assertEqual(status, 200)
        status, out = self.server.call("DELETE", f"/api/license-map/{row['id']}", reviewer="alice")
        self.assertEqual(status, 200)
        self.assertEqual(out["deleted"], row["id"])
        status, body = self.server.call("GET", "/api/license-map")
        self.assertEqual(status, 200)
        self.assertFalse(any(r["short_name"] == "MIT-ALT" for r in body["items"]))


class ApplyAndReadOnlyTests(unittest.TestCase):
    def setUp(self):
        from clreview.db import DB
        from clreview.server import seed_demo
        self.db = DB(":memory:")
        self.mapping_id = seed_demo(self.db)["mapping_id"]
        self.server = Server(self.db, read_only=True)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_per_component_apply_with_read_only_refusal(self):
        status, queue = self.server.call("GET", f"/api/mappings/{self.mapping_id}/components?q=zlib")
        items = [i for i in queue["items"] if i["pl_name"] == "zlib" and i["status"] != "unmatched"]
        self.assertTrue(items)
        mid = items[0]["id"]
        status, err = self.server.call("POST", f"/api/components/{mid}/apply",
                                       {"confirm": True}, reviewer="alice")
        self.assertEqual(status, 409)
        self.assertTrue(err["read_only"])

    def test_direct_apply_marks_reviewed_and_closes_upload(self):
        self.dir = tmpdir()
        svc, _db, mid = make_service(self.dir, *components())
        review, sync = make_review(svc)
        match_id = review.list_components(mid)["items"][0]["id"]
        approve_all(review, match_id)
        out = sync.apply_component(match_id, confirm=True, actor="alice")
        self.assertTrue(out["reviewed"])
        self.assertEqual(out["job"]["status"], "completed")
        self.assertEqual(review.match(match_id)["review_state"], "reviewed")
        pl = svc.connector_for(svc.mapping(mid)["pl_connection_id"]).read_component("proj|1.0|zlib")
        self.assertEqual(pl["reviewStatus"], "REVIEWED")
        sl = svc.connector_for(svc.mapping(mid)["sl_connection_id"])
        self.assertEqual(sl.upload_status("scan"), "Closed")
        self.assertEqual(sl.scans[0]["comment"], "reviewed")


if __name__ == "__main__":
    unittest.main()
