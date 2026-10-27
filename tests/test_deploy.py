"""Deploy tests: coordinate mapping, id2url contract, folder naming, deploy report states."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from clreview import deploy as deploy_mod
from clreview.connectors import fossology as foss_mod
from clreview.connectors.base import ConnectorError
from clreview.connectors.fossology import FossologyConnector
from clreview.deploy import (STATUS_ALREADY, STATUS_FAILED, STATUS_NO_URL, STATUS_UPLOADED,
                             STATUS_UNSUPPORTED, STATUS_WOULD_UPLOAD, coordinate_from_origin,
                             coordinate_from_purl, deploy, run_id2url, sanitize_folder_name)

from helpers import tmpdir  # noqa: F401  (kept for parity with the other test modules)


class CoordinateTests(unittest.TestCase):
    def test_purl_to_id2url_coordinate(self):
        cases = {
            "pkg:maven/com.google.guava/guava@31.1-jre": ("maven", "com.google.guava:guava:31.1-jre"),
            "pkg:npm/lodash@4.17.21": ("npm", "lodash/4.17.21"),
            "pkg:npm/@babel/core@7.22.0": ("npm", "@babel/core/7.22.0"),
            "pkg:npm/%40babel/core@7.22.0": ("npm", "@babel/core/7.22.0"),
            "pkg:pypi/requests@2.31.0": ("pypi", "requests/2.31.0"),
            "pkg:cargo/serde@1.0.188": ("crates", "serde/1.0.188"),
            "pkg:nuget/Newtonsoft.Json@13.0.3": ("nuget", "Newtonsoft.Json/13.0.3"),
            "pkg:gem/rails@7.1.2": ("rubygems", "rails/7.1.2"),
            "pkg:golang/github.com/gin-gonic/gin@v1.9.1": ("go", "github.com/gin-gonic/gin/v1.9.1"),
            "pkg:composer/laravel/framework@v10.35.0": ("packagist", "laravel/framework/v10.35.0"),
            "pkg:cocoapods/Alamofire@5.8.1": ("cocoapods", "Alamofire/5.8.1"),
            "pkg:hex/phoenix@1.7.10": ("hex", "phoenix/1.7.10"),
            "pkg:pub/flutter_bloc@8.1.3": ("dart", "flutter_bloc/8.1.3"),
        }
        for purl, expected in cases.items():
            self.assertEqual(coordinate_from_purl(purl), expected, purl)

    def test_unsupported_or_incomplete_purl(self):
        self.assertIsNone(coordinate_from_purl("pkg:deb/debian/openssl@3"))
        self.assertIsNone(coordinate_from_purl("pkg:npm/lodash"))
        self.assertIsNone(coordinate_from_purl(None))
        self.assertIsNone(coordinate_from_purl("lodash/4.17.21"))

    def test_blackduck_origin_fallback_uses_component_version(self):
        self.assertEqual(coordinate_from_origin("maven:com.google.guava:guava:31.1-jre"),
                         ("maven", "com.google.guava:guava:31.1-jre"))
        self.assertEqual(coordinate_from_origin("maven:com.google.guava:guava", "31.1-jre"),
                         ("maven", "com.google.guava:guava:31.1-jre"))
        self.assertEqual(coordinate_from_origin("npm:lodash", "4.17.21"), ("npm", "lodash/4.17.21"))
        self.assertIsNone(coordinate_from_origin("npm:lodash"))
        self.assertIsNone(coordinate_from_origin("custom:thing"))

    def test_folder_name_sanitizing(self):
        self.assertEqual(sanitize_folder_name("Spring Sample/1.2"), "Spring_Sample_1.2")
        self.assertEqual(sanitize_folder_name('a:b*c?"d'), "a_b_c__d")


class Id2UrlContractTests(unittest.TestCase):
    def test_batch_output_is_mapped_by_registry_and_coordinate(self):
        payload = {"results": [{"registry": "npm", "origin_id": "lodash/4.17.21",
                                "package_name": "lodash", "version": "4.17.21",
                                "download_url": "https://r/lodash.tgz"}],
                   "errors": [{"line": 2, "error": "not found", "input": "pypi\tmissing/1.0"}]}
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(deploy_mod.shutil, "which", return_value="/usr/bin/id2url"), \
                mock.patch.object(deploy_mod.subprocess, "run", return_value=proc) as run:
            out = run_id2url([("npm", "lodash/4.17.21"), ("pypi", "missing/1.0")])
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["id2url", "batch"])
        self.assertIn("--continue-on-error", argv)
        self.assertEqual(out["urls"]["npm\tlodash/4.17.21"], "https://r/lodash.tgz")
        self.assertEqual(out["errors"][0]["input"], "pypi\tmissing/1.0")

    def test_missing_binary_is_a_clear_error(self):
        with mock.patch.object(deploy_mod.shutil, "which", return_value=None):
            with self.assertRaises(ConnectorError) as ctx:
                run_id2url([("npm", "lodash/4.17.21")])
        self.assertIn("id2url", str(ctx.exception))


class FakePL:
    def __init__(self, items, label=("Spring Sample", "1.2")):
        self.items, self.label = items, label

    def source_coordinates(self, project_id, version_id):
        return self.items

    def version_label(self, project_id, version_id):
        return {"project": self.label[0], "version": self.label[1]}


class FakeSL:
    def __init__(self, existing=(), fail_for=()):
        self.config = {}
        self.existing = list(existing)
        self.fail_for = list(fail_for)
        self.uploads = []
        self.folders_created = []

    def ensure_folder(self, name, parent_id):
        self.folders_created.append((name, parent_id))
        return {"id": 42, "name": name, "created": True}

    def list_uploads(self, folder_id):
        return [{"uploadname": n} for n in self.existing]

    def upload_url(self, folder_id, url, name, main_license=None, agents=None):
        if name in self.fail_for:
            raise ConnectorError(f"FOSSology rejected {name}", status=400)
        self.uploads.append({"folder": folder_id, "url": url, "name": name, "agents": agents})
        return {"status": 201, "id": len(self.uploads), "message": "Upload is created"}


def fake_service(pl=None, sl=None):
    audit = []
    conns = {1: pl, 2: sl}
    svc = SimpleNamespace(db=SimpleNamespace(audit=lambda *a: audit.append(a)),
                          connector_for=lambda cid: conns[cid])
    return svc, audit


def run(pl_items, sl, resolver, **kw):
    svc, audit = fake_service(pl=FakePL(pl_items), sl=sl)
    report = deploy(svc, pl_connection_id=1, sl_connection_id=2, project_id="p", version_id="v",
                    actor="reviewer", resolver=resolver, **kw)
    return report, svc, audit


MAVEN = {"name": "guava", "version": "31.1-jre",
         "origins": [{"purl": "pkg:maven/com.google.guava/guava@31.1-jre"}]}
NPM = {"name": "lodash", "version": "4.17.21", "origins": [{"purl": "pkg:npm/lodash@4.17.21"}]}
UNKNOWN = {"name": "vendor-thing", "version": "9", "origins": [{"origin": "custom:thing"}]}


class DeployTests(unittest.TestCase):
    def resolver(self, urls, errors=None):
        return lambda coords: {"urls": {f"{r}\t{c}": u for (r, c), u in urls.items()},
                               "errors": errors or []}

    def test_dry_run_reports_without_touching_fossology(self):
        sl = FakeSL()
        report, _, _ = run([MAVEN, NPM], sl,
                           self.resolver({("maven", "com.google.guava:guava:31.1-jre"): "https://m/g.jar",
                                          ("npm", "lodash/4.17.21"): "https://n/l.tgz"}),
                           dry_run=True)
        self.assertEqual(report["folder"]["name"], "Spring_Sample__1.2")
        self.assertEqual([r["status"] for r in report["components"]],
                         [STATUS_WOULD_UPLOAD, STATUS_WOULD_UPLOAD])
        self.assertEqual(sl.uploads, [])
        self.assertEqual(sl.folders_created, [])

    def test_upload_creates_folder_once_and_names_uploads(self):
        sl = FakeSL()
        report, _, audit = run([MAVEN, NPM], sl,
                               self.resolver({("maven", "com.google.guava:guava:31.1-jre"): "https://m/g.jar",
                                              ("npm", "lodash/4.17.21"): "https://n/l.tgz"}),
                               dry_run=False, parent_folder=7)
        self.assertEqual(sl.folders_created, [("Spring_Sample__1.2", 7)])
        self.assertEqual([u["name"] for u in sl.uploads], ["guava-31.1-jre", "lodash-4.17.21"])
        self.assertEqual([r["status"] for r in report["components"]], [STATUS_UPLOADED, STATUS_UPLOADED])
        self.assertEqual(report["counts"][STATUS_UPLOADED], 2)
        self.assertEqual(audit[0][1], "deploy.fossology")

    def test_unresolved_and_unsupported_components_are_per_row_not_fatal(self):
        sl = FakeSL()
        report, _, _ = run([MAVEN, UNKNOWN], sl,
                           self.resolver({}, errors=[{"line": 1, "error": "not found in registry",
                                                      "input": "maven\tcom.google.guava:guava:31.1-jre"}]),
                           dry_run=False, parent_folder=7)
        by_name = {r["name"]: r for r in report["components"]}
        self.assertEqual(by_name["guava"]["status"], STATUS_NO_URL)
        self.assertIn("not found", by_name["guava"]["message"])
        self.assertEqual(by_name["vendor-thing"]["status"], STATUS_UNSUPPORTED)
        self.assertEqual(sl.uploads, [])

    def test_existing_upload_is_not_uploaded_twice(self):
        sl = FakeSL(existing=["lodash-4.17.21"])
        report, _, _ = run([MAVEN, NPM], sl,
                           self.resolver({("maven", "com.google.guava:guava:31.1-jre"): "https://m/g.jar",
                                          ("npm", "lodash/4.17.21"): "https://n/l.tgz"}),
                           dry_run=False, parent_folder=7)
        self.assertEqual([u["name"] for u in sl.uploads], ["guava-31.1-jre"])
        by_name = {r["name"]: r for r in report["components"]}
        self.assertEqual(by_name["lodash"]["status"], STATUS_ALREADY)

    def test_same_source_for_two_components_uploads_once(self):
        twin = {"name": "guava-android", "version": "31.1-jre",
                "origins": [{"purl": "pkg:maven/com.google.guava/guava@31.1-jre"}]}
        sl = FakeSL()
        report, _, _ = run([MAVEN, twin], sl,
                           self.resolver({("maven", "com.google.guava:guava:31.1-jre"): "https://m/g.jar"}),
                           dry_run=False, parent_folder=7)
        self.assertEqual(len(sl.uploads), 1)
        statuses = [r["status"] for r in report["components"]]
        self.assertEqual(sorted(statuses), [STATUS_ALREADY, STATUS_UPLOADED])

    def test_upload_failure_is_recorded_per_component(self):
        sl = FakeSL(fail_for=["guava-31.1-jre"])
        report, _, _ = run([MAVEN], sl,
                           self.resolver({("maven", "com.google.guava:guava:31.1-jre"): "https://m/g.jar"}),
                           dry_run=False, parent_folder=7)
        self.assertEqual(report["components"][0]["status"], STATUS_FAILED)
        self.assertIn("rejected", report["components"][0]["message"])

    def test_parent_folder_is_required_for_a_real_run(self):
        with self.assertRaises(ConnectorError) as ctx:
            run([MAVEN], FakeSL(), self.resolver({}), dry_run=False)
        self.assertIn("parent folder", str(ctx.exception))

    def test_custom_folder_name_wins(self):
        sl = FakeSL()
        report, _, _ = run([MAVEN], sl, self.resolver({}), folder="Q3 audit", dry_run=False, parent_folder=3)
        self.assertEqual(sl.folders_created, [("Q3_audit", 3)])
        self.assertEqual(report["folder"]["name"], "Q3_audit")

    def test_id2url_binary_is_configurable_per_connection(self):
        pl = FakePL([MAVEN])
        pl.config = {"id2url_command": "/opt/tools/id2url"}
        svc, _ = fake_service(pl=pl, sl=FakeSL())
        seen = {}

        def spy(coords, command="id2url", **kw):
            seen["command"] = command
            return {"urls": {}, "errors": []}
        with mock.patch.object(deploy_mod, "run_id2url", spy):
            deploy(svc, pl_connection_id=1, sl_connection_id=2, project_id="p", version_id="v",
                   actor="reviewer", dry_run=True)
        self.assertEqual(seen["command"], "/opt/tools/id2url")


class FossologyDeployOpsTests(unittest.TestCase):
    def conn(self):
        return FossologyConnector("https://fossy.example", {"token": "t"}, {})

    def test_ensure_folder_reuses_existing(self):
        c = self.conn()
        with mock.patch.object(c, "list_folders", return_value=[{"id": 9, "name": "P__1", "parent": 7}]), \
                mock.patch.object(c, "_post") as post:
            self.assertEqual(c.ensure_folder("P__1", 7), {"id": 9, "name": "P__1", "created": False})
            post.assert_not_called()

    def test_ensure_folder_creates_then_resolves_id(self):
        c = self.conn()
        listings = [[], [{"id": 11, "name": "P__1", "parent": 7}]]
        with mock.patch.object(c, "list_folders", side_effect=listings), \
                mock.patch.object(c, "_post", return_value=(201, {"message": "11"})) as post:
            self.assertEqual(c.ensure_folder("P__1", 7), {"id": 11, "name": "P__1", "created": True})
        self.assertEqual(post.call_args[0][2], {"parentFolder": "7", "folderName": "P__1"})

    def test_upload_url_body_and_headers(self):
        c = self.conn()
        with mock.patch.object(foss_mod, "request") as req:
            req.return_value = (201, {}, b'{"code":201,"message":"Upload is created"}')
            out = c.upload_url(42, "https://m/g.jar", "guava-31.1-jre",
                               agents=["nomos", "copyright_email_author"])
        method, url = req.call_args[0][:2]
        headers, body = req.call_args[1]["headers"], req.call_args[1]["body"]
        self.assertEqual((method, url), ("POST", "https://fossy.example/uploads"))
        self.assertEqual((headers["folderId"], headers["uploadType"]), ("42", "url"))
        self.assertEqual(req.call_args[1]["retries"], 0)
        self.assertEqual(body["location"], {"url": "https://m/g.jar", "name": "guava-31.1-jre"})
        self.assertEqual(body["scanOptions"]["analysis"], {"nomos": True, "copyright_email_author": True})
        self.assertEqual(out["message"], "Upload is created")

    def test_folder_scan_aggregates_uploads(self):
        c = self.conn()
        from clreview.domain import ComponentData

        def upload_inventory(upload_id):
            # the archive's own directory name is meaningless next to the deployer's upload name
            return [ComponentData(external_id="com", name="com", version=None)]
        with mock.patch.object(c, "list_uploads", return_value=[{"id": 3, "uploadname": "guava-31.1-jre"}]), \
                mock.patch.object(c, "_upload_inventory", side_effect=upload_inventory):
            out = c.fetch_inventory("folder:5")
        self.assertEqual([(x.external_id, x.name, x.version) for x in out],
                         [("3|com", "guava", "31.1-jre")])
        self.assertEqual(out[0].metadata["upload_name"], "guava-31.1-jre")

    def test_version_qualifier_survives_the_name_parse(self):
        from clreview.domain import split_component_dir
        self.assertEqual(split_component_dir("guava-31.1-jre"), ("guava", "31.1-jre"))
        self.assertEqual(split_component_dir("lodash-4.17.21"), ("lodash", "4.17.21"))
        self.assertEqual(split_component_dir("pkg-1.2.3.tar.gz"), ("pkg", "1.2.3"))

    def test_folders_are_listed_as_scans(self):
        c = self.conn()
        with mock.patch.object(c, "_pages", return_value=[{"id": 1, "uploadname": "solo"}]), \
                mock.patch.object(c, "list_folders", return_value=[{"id": 5, "name": "P__1"}]):
            scans = {s.external_id: s.name for s in c.list_scans()}
        self.assertEqual(scans["1"], "solo")
        self.assertEqual(scans["folder:5"], "P__1 (folder)")


class BlackDuckCoordinatesTests(unittest.TestCase):
    def test_source_coordinates_reads_identity_only(self):
        from clreview.connectors.blackduck import BlackDuckConnector
        c = BlackDuckConnector("https://bd.example", {"token": "t"}, {})
        page = {"totalCount": 1, "items": [{
            "componentName": "guava", "componentVersionName": "31.1-jre",
            "origins": [{"packageUrl": "pkg:maven/com.google.guava/guava@31.1-jre",
                         "externalNamespace": "maven", "externalId": "com.google.guava:guava"}],
            "_meta": {"href": "https://bd/api/projects/p/versions/v/components/c"}}]}
        with mock.patch.object(c, "_get", return_value=page):
            out = c.source_coordinates("p", "v")
        self.assertEqual(out[0]["name"], "guava")
        self.assertEqual(out[0]["origins"][0]["purl"], "pkg:maven/com.google.guava/guava@31.1-jre")
        self.assertEqual(out[0]["bom_href"], "https://bd/api/projects/p/versions/v/components/c")


class DeployEndpointTests(unittest.TestCase):
    """The HTTP route is a thin pass-through; this checks it stays one and keeps the reviewer."""

    def test_post_deploy_routes_to_the_deploy_module(self):
        from clreview.api import Api
        from clreview.db import DB
        captured = {}

        def fake_deploy(svc, **kw):
            captured.update(kw)
            return {"ok": True}
        db = DB(":memory:")
        api = Api(db)
        body = {"pl_connection_id": 1, "sl_connection_id": 2, "project_id": "p", "version_id": "v",
                "folder": "Q3", "parent_folder": 9, "agents": ["nomos"], "dry_run": False}
        try:
            with mock.patch("clreview.deploy.deploy", fake_deploy):
                status, out = api.handle("POST", "/api/deploy", {}, body, "bob")
        finally:
            db.close()
        self.assertEqual((status, out), (200, {"ok": True}))
        self.assertEqual(captured["folder"], "Q3")
        self.assertEqual(captured["parent_folder"], 9)
        self.assertEqual(captured["dry_run"], False)
        self.assertEqual(captured["actor"], "bob")
