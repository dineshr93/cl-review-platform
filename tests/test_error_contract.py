"""C5: every guard says which rule it enforces, and only that guard answers for it.

Each test pins one `raise` in clreview/ that no test reached before. The message is asserted
verbatim, not as a substring, because a substring like "confirm" or "id2url" is matched by any
message containing that word — which is exactly how a copy-pasted guard survives a substring test.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from clreview import deploy as deploy_mod
from clreview.connectors.base import REGISTRY, ConnectorError, create_connector
from clreview.connectors.mock import MockPLConnector, MockSLConnector
from clreview.connectors.scancode import ScanCodeConnector
from clreview.db import DB
from clreview.deploy import deploy, run_id2url
from clreview.domain import Capabilities
from clreview.service import Service

from helpers import fixture_set, make_review, make_service, pl_component, tmpdir
from test_e2e import Server
from test_review import pair_components
from test_sync import approve_all, components

# No shipped source-level tool lacks folders (FOSSology and the mock both have them), so the
# "this tool has no folder concept" guard is only reachable through a tool that does: ScanCode,
# registered under the sl role for this module only.
FOLDERLESS_SL = "scancode-sl"
REGISTRY.setdefault(FOLDERLESS_SL, {
    "roles": ("sl",),
    "factory": lambda base_url, config, auth_env: ScanCodeConnector(base_url, config, auth_env),
    "description": "test-only source-level tool with no folder concept",
})

NO_WRITE = Capabilities(
    read_components=True, read_copyrights=True, read_license_text=True,
    write_copyrights=False, write_license_names=False, write_license_text=False,
    verify_writes=False, notes="",
)
# sync.py raises `caps.notes or <fallback>`: with empty notes the fallback is what the operator reads.
FALLBACK = {"copyright": "copyright writes unsupported",
            "license_name": "license name writes unsupported",
            "license_text": "license text writes unsupported"}


class ServiceGuardTests(unittest.TestCase):
    """The connection and mapping validation the API answers with a 400."""

    def setUp(self):
        self.dir = tmpdir()
        self.paths = fixture_set(self.dir, [], [])
        self.db = DB(":memory:")
        self.addCleanup(self.db.close)
        self.svc = Service(self.db)
        self.pl = self.svc.add_connection("pl", "mock-pl", "PL", "", {"fixtures": self.paths["pl"]}, {})
        self.sl = self.svc.add_connection("sl", "mock-sl", "SL", "", {"fixtures": self.paths["sl"]}, {})

    def test_role_is_pl_or_sl_and_says_which(self):
        with self.assertRaises(ValueError) as ctx:
            self.svc.add_connection("middle", "mock-pl", "nope", "", {}, {})
        self.assertEqual(str(ctx.exception), "role must be 'pl' or 'sl'")

    def test_discovery_refuses_the_wrong_side_of_the_pair(self):
        cases = (
            (lambda: self.svc.list_projects(self.sl["id"]),
             "project discovery requires a project-level connection"),
            (lambda: self.svc.list_scans(self.pl["id"]),
             "scan discovery requires a source-level connection"),
            (lambda: self.svc.list_folders(self.pl["id"]),
             "folder discovery requires a source-level connection"),
        )
        for call, message in cases:
            with self.assertRaises(ValueError) as ctx:
                call()
            self.assertEqual(str(ctx.exception), message)

    def test_a_source_tool_without_a_folder_concept_names_itself(self):
        scan = os.path.join(self.dir, "scan.json")
        with open(scan, "w", encoding="utf-8") as fh:
            json.dump({"files": []}, fh)
        conn = self.svc.add_connection("sl", FOLDERLESS_SL, "no folders", "", {"json_path": scan}, {})
        with self.assertRaises(ValueError) as ctx:
            self.svc.list_folders(conn["id"])
        self.assertEqual(str(ctx.exception), f"{FOLDERLESS_SL} has no folder concept to deploy into")

    def test_a_mapping_needs_one_connection_of_each_side(self):
        with self.assertRaises(ValueError) as ctx:
            self.svc.create_mapping("bad", self.sl["id"], "proj", "1.0", self.sl["id"], "scan", "t")
        self.assertEqual(str(ctx.exception), "mapping requires one PL connection and one SL connection")


class ReviewGuardTests(unittest.TestCase):
    """Field/action vocabulary and the two lookups that answer 404."""

    def setUp(self):
        self.dir = tmpdir()
        pl, sl = pair_components()
        self.svc, self.db, self.mid = make_service(self.dir, pl, sl)
        self.addCleanup(self.db.close)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]

    def test_unknown_field_and_action_are_quoted_back(self):
        cases = (
            (lambda: self.review.decide(self.match_id, "colour", "k", "approve_sl"),
             "unknown field 'colour'"),
            (lambda: self.review.decide(self.match_id, "copyright", "k", "obliterate"),
             "unknown action 'obliterate'"),
            (lambda: self.review.add_suggestion(self.match_id, "colour", "k", "value"),
             "unknown field 'colour'"),
        )
        for call, message in cases:
            with self.assertRaises(ValueError) as ctx:
                call()
            self.assertEqual(str(ctx.exception), message)

    def test_a_suggestion_answer_is_accept_or_reject(self):
        sid = self.review.add_suggestion(
            self.match_id, "copyright", "k", "Copyright 1995-2017 Jean-loup Gailly")["id"]
        with self.assertRaises(ValueError) as ctx:
            self.review.decide_suggestion(sid, "maybe")
        self.assertEqual(str(ctx.exception), "suggestion decision must be accept or reject")

    def test_a_missing_suggestion_is_a_404_with_a_plain_message(self):
        with self.assertRaises(KeyError) as ctx:
            self.review.decide_suggestion(999999, "accept")
        self.assertEqual(str(ctx.exception), "'suggestion 999999 not found'")  # KeyError reprs its arg

        server = Server(self.db)
        self.addCleanup(server.close)
        status, body = server.call("POST", "/api/suggestions/999999/decision", {"action": "accept"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "suggestion 999999 not found")

    def test_review_state_vocabulary_is_closed(self):
        with self.assertRaises(ValueError) as ctx:
            self.review.set_review_state(self.match_id, "half_done")
        self.assertEqual(str(ctx.exception), "invalid review state")


class SyncGuardTests(unittest.TestCase):
    """The retry guards nothing exercised, and the write refusals stored on a row."""

    def setUp(self):
        self.dir = tmpdir()
        self.svc, self.db, self.mid = make_service(self.dir, *components())
        self.addCleanup(self.db.close)
        self.review, self.sync = make_review(self.svc)
        self.match_id = self.review.list_components(self.mid)["items"][0]["id"]
        approve_all(self.review, self.match_id)

    def pl_connector(self):
        return self.svc.connector_for(self.svc.mapping(self.mid)["pl_connection_id"])

    def job_where_every_write_fails(self):
        """A live job whose every item is `failed`, so retry re-queues all of them."""
        boom = ConnectorError("simulated PL API failure (500)", status=500)
        with mock.patch.multiple(self.pl_connector(),
                                 update_copyrights=mock.Mock(side_effect=boom),
                                 update_licenses=mock.Mock(side_effect=boom),
                                 update_license_text=mock.Mock(side_effect=boom)):
            return self.sync.create_job(self.mid, dry_run=False, confirm=True)

    def test_retrying_a_dry_run_job_says_make_a_live_one(self):
        job = self.sync.create_job(self.mid, dry_run=True)
        with self.assertRaises(ValueError) as ctx:
            self.sync.retry(job["id"], confirm=True)
        self.assertEqual(str(ctx.exception), "dry-run jobs have nothing to retry; create a live job")

    def test_retry_requires_confirm(self):
        job = self.job_where_every_write_fails()
        self.assertTrue(any(i["status"] == "failed" for i in job["items"]))
        with self.assertRaises(ValueError) as ctx:
            self.sync.retry(job["id"])
        self.assertEqual(str(ctx.exception), "retry requires confirm=true")

    def test_retrying_a_job_with_nothing_failed_says_so(self):
        job = self.sync.create_job(self.mid, dry_run=False, confirm=True)
        self.assertNotIn("failed", {i["status"] for i in job["items"]})
        with self.assertRaises(ValueError) as ctx:
            self.sync.retry(job["id"], confirm=True)
        self.assertEqual(str(ctx.exception), "no failed or interrupted items to retry")

    def test_a_write_the_target_cannot_do_is_recorded_on_the_item(self):
        """sync.py raises inside _execute, so the refusal lands on the row, never as an exception.

        The item is re-queued by retry() (status 'ready'), which is the only path that reaches the
        capability check: preview_match already stamps unsupported_target for a capability that is
        off, and _execute skips those statuses.
        """
        job = self.job_where_every_write_fails()
        with mock.patch.object(self.pl_connector(), "capabilities", NO_WRITE):
            retry = self.sync.retry(job["id"], confirm=True)
        rows = {i["field"]: i for i in retry["items"]}
        self.assertEqual(set(rows), set(FALLBACK))
        for field, item in rows.items():
            self.assertEqual(item["status"], "unsupported_target", field)
            self.assertEqual(item["message"], FALLBACK[field])

    def test_the_connectors_own_note_beats_the_generic_fallback(self):
        job = self.job_where_every_write_fails()
        noted = dataclasses.replace(NO_WRITE, notes="target tool has no write endpoints")
        with mock.patch.object(self.pl_connector(), "capabilities", noted):
            retry = self.sync.retry(job["id"], confirm=True)
        self.assertTrue(retry["items"])
        for item in retry["items"]:
            self.assertEqual(item["status"], "unsupported_target")
            self.assertEqual(item["message"], "target tool has no write endpoints")


class DeployGuardTests(unittest.TestCase):
    """The id2url subprocess boundary and the deploy capability guard."""

    COORDS = [("maven", "com.google.guava:guava:31.1-jre")]

    def run_batch(self, **kw):
        with mock.patch.object(deploy_mod.shutil, "which", return_value="/usr/bin/id2url"):
            return run_id2url(self.COORDS, **kw)

    def test_a_timed_out_batch_names_the_limit(self):
        with mock.patch.object(deploy_mod.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd=["id2url", "batch"], timeout=5)):
            with self.assertRaises(ConnectorError) as ctx:
                self.run_batch(timeout=5)
        self.assertEqual(str(ctx.exception), "id2url batch timed out after 5s")

    def test_an_empty_batch_carries_the_exit_code_and_stderr(self):
        proc = SimpleNamespace(returncode=1, stdout="", stderr="fatal: boom")
        with mock.patch.object(deploy_mod.subprocess, "run", return_value=proc):
            with self.assertRaises(ConnectorError) as ctx:
                self.run_batch()
        self.assertEqual(str(ctx.exception), "id2url batch produced no output (exit 1): fatal: boom")

    def test_unparsable_batch_output_is_shown_not_swallowed(self):
        proc = SimpleNamespace(returncode=0, stdout="not json", stderr="")
        with mock.patch.object(deploy_mod.subprocess, "run", return_value=proc):
            with self.assertRaises(ConnectorError) as ctx:
                self.run_batch()
        self.assertEqual(str(ctx.exception), "id2url batch returned unparsable output: not json")

    def test_a_connection_that_cannot_deploy_names_the_missing_capability(self):
        # MockSLConnector can ensure_folder but has no source_coordinates, so as the PL side it
        # reaches the guard the test doubles in test_deploy.py always satisfied with hasattr.
        sl = MockSLConnector("", {"fixtures": fixture_set(tmpdir(), [], [])["sl"]}, {})
        svc = SimpleNamespace(db=SimpleNamespace(audit=lambda *a: None),
                              connector_for=lambda cid: sl)
        with self.assertRaises(ConnectorError) as ctx:
            deploy(svc, pl_connection_id=1, sl_connection_id=2, project_id="p", version_id="v",
                   actor="reviewer", dry_run=True)
        self.assertEqual(str(ctx.exception),
                         "the pl connection cannot deploy (missing source_coordinates())")


class CreateConnectorTests(unittest.TestCase):
    """The registry refuses a tool it does not know, and a tool in a role it cannot play."""

    def test_unknown_tool_lists_the_known_ones(self):
        with self.assertRaises(ConnectorError) as ctx:
            create_connector("nope", "pl", "https://x", {}, {})
        self.assertEqual(str(ctx.exception), f"unknown tool 'nope'; known: {sorted(REGISTRY)}")

    def test_a_tool_in_the_wrong_role_lists_its_roles(self):
        with self.assertRaises(ConnectorError) as ctx:
            create_connector("mock-sl", "pl", "https://x", {}, {})
        self.assertEqual(str(ctx.exception),
                         "tool 'mock-sl' cannot act as 'pl'; supported roles: ('sl',)")


class MockFixtureGuardTests(unittest.TestCase):
    """The mock's own failure fixture — the one the sync tests should reach for."""

    def setUp(self):
        self.dir = tmpdir()
        self.paths = fixture_set(self.dir, [pl_component(name="lib", version="1.0", fail_writes_once=True)], [])
        self.conn = MockPLConnector("", {"fixtures": self.paths["pl"]}, {})

    def test_a_malformed_component_key_is_refused(self):
        with self.assertRaises(ConnectorError) as ctx:
            self.conn.read_component("nonsense")
        self.assertEqual(str(ctx.exception), "bad component key 'nonsense'")

    def test_the_fixture_fails_the_first_write_once(self):
        key = "proj|1.0|lib"
        with self.assertRaises(ConnectorError) as ctx:
            self.conn.update_licenses(key, [{"name": "MIT"}])
        self.assertEqual(str(ctx.exception), "simulated PL API failure (500)")
        self.assertEqual(self.conn.update_licenses(key, [{"name": "MIT"}])["status"], 200)


class ScanCodeGuardTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()

    def test_a_scancode_file_must_be_pointed_at(self):
        with self.assertRaises(ConnectorError) as ctx:
            ScanCodeConnector("", {}, {})
        self.assertEqual(str(ctx.exception),
                         "ScanCode connector requires config.json_path (or its env variable)")

    def test_a_missing_scancode_file_names_the_path(self):
        missing = os.path.join(self.dir, "absent.json")
        conn = ScanCodeConnector("", {"json_path": missing}, {})
        with self.assertRaises(ConnectorError) as ctx:
            conn.test_connection()
        self.assertEqual(str(ctx.exception), f"ScanCode JSON not found: {missing}")


class FossIDPlaceholderTests(unittest.TestCase):
    """Every FOSSID operation is a placeholder, and each one says which."""

    EXPECTED = {
        "test_connection": "FOSSID adapter not implemented for test_connection (no verified API mapping)",
        "list_projects": "FOSSID adapter not implemented for list_projects (no verified API mapping)",
        "list_versions": "FOSSID adapter not implemented for list_versions (no verified API mapping)",
        "fetch_inventory": "FOSSID adapter not implemented for fetch_inventory (no verified API mapping)",
    }

    def setUp(self):
        self.conn = create_connector("fossid", "pl", "https://fossid.example", {}, {})

    def test_each_unimplemented_operation_names_itself(self):
        calls = {
            "test_connection": lambda: self.conn.test_connection(),
            "list_projects": lambda: self.conn.list_projects(),
            "list_versions": lambda: self.conn.list_versions("p"),
            "fetch_inventory": lambda: self.conn.fetch_inventory("p", "v"),
        }
        for op, call in calls.items():
            with self.subTest(op=op):
                with self.assertRaises(ConnectorError) as ctx:
                    call()
                self.assertEqual(str(ctx.exception), self.EXPECTED[op])
                self.assertTrue(ctx.exception.unsupported)


if __name__ == "__main__":
    unittest.main()
