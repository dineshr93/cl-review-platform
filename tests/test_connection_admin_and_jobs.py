import tempfile
import threading
import time
import unittest

from clreview.connectors.base import RefreshCancelled
from clreview.jobs import RefreshJobs

from helpers import make_service


class ConnectionAdminAndJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.db, self.mid = make_service(self.tmp.name, [], [])

    def test_edit_keeps_role_and_tool_and_resets_the_test_state(self):
        cid = self.svc.mapping(self.mid)["pl_connection_id"]
        fixtures = self.svc.connection(cid)["config"]["fixtures"]
        out = self.svc.update_connection(cid, "renamed", "", {"fixtures": fixtures}, {})
        self.assertEqual((out["name"], out["role"], out["tool"]), ("renamed", "pl", "mock-pl"))
        self.assertFalse(out["last_test_ok"])
        with self.assertRaises(ValueError):
            self.svc.update_connection(cid, "x", "", {"token": "secret"}, {})

    def test_delete_is_refused_while_a_mapping_uses_the_connection(self):
        cid = self.svc.mapping(self.mid)["pl_connection_id"]
        with self.assertRaisesRegex(ValueError, "test mapping"):
            self.svc.delete_connection(cid)
        spare = self.svc.add_connection("sl", "mock-sl", "spare", "", {}, {})
        self.assertEqual(self.svc.delete_connection(spare["id"]), {"deleted": spare["id"]})
        with self.assertRaises(KeyError):
            self.svc.connection(spare["id"])

    def test_background_refresh_finishes_and_can_be_cancelled(self):
        jobs = RefreshJobs(self.svc)
        job = jobs.start(self.mid, "tester")
        for _ in range(100):
            job = jobs.get(job["id"])
            if job["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "done")
        self.assertIn("sides", job["result"])

        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(RefreshCancelled):
            self.svc.refresh(self.mid, "tester", cancel=cancel)


if __name__ == "__main__":
    unittest.main()
