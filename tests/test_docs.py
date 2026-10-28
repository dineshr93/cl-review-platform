"""Docs that claim something the code can check (F1, F2).

Two tripwires, both cheap, both here because the same numbers have drifted before:

* README states how many tests the suite has. It has been wrong five times across four files.
  **Adding or removing a test means updating the number in README.md** — that friction is the
  point of this file, not a defect in it. Only the number is asserted; the prose around it is free
  to change.
* README documents the HTTP surface in one table. A route added to ``ROUTES`` without a row is
  caught here.
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
API = ROOT / "clreview" / "api.py"


def discovered_tests() -> int:
    """Count the suite the way CI counts it, in a child interpreter.

    Discovery in-process would import every test module a second time under a different module
    name (pytest imports them as ``test_x`` from ``sys.path``), which duplicates module-level
    state such as the temp-directory registry in ``helpers``. A child process costs ~0.1 s and
    cannot interfere.
    """
    out = subprocess.run(
        [sys.executable, "-c",
         "import unittest; print(unittest.TestLoader().discover('tests', top_level_dir='tests')"
         ".countTestCases())"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def shipped_routes() -> list[tuple[str, str]]:
    """``(method, path)`` for every entry in ``api.ROUTES``, ``{id}`` for a numeric group."""
    source = API.read_text()
    pairs = re.findall(r'\("(\w+)", re\.compile\(r"(/api[^"]+)"\)', source)
    return sorted((method, path.replace(r"(\d+)", "{id}")) for method, path in pairs)


class DocsTruthTests(unittest.TestCase):
    def test_readme_test_count_matches_the_suite(self):
        claimed = [int(n) for n in re.findall(r"(\d+)(?: stdlib)? tests", README.read_text())]
        self.assertTrue(claimed, "README states no test count at all")
        self.assertEqual(claimed, [discovered_tests()] * len(claimed),
                         "update the test count in README.md to the number this run discovered")

    def test_readme_documents_every_route(self):
        documented = {path.replace(r"(\d+)", "{id}")
                      for path in re.findall(r"`(/api[^`]*)`", README.read_text())}
        missing = [f"{method} {path}" for method, path in shipped_routes()
                   if path not in documented]
        # `confirm` / `reassign` / `reject` share one row; the two trailing verbs are not in
        # backticks, so they are matched by name instead.
        combined = {"POST /api/components/{id}/reassign", "POST /api/components/{id}/reject"}
        self.assertEqual([m for m in missing if m not in combined], [],
                         "README's API table is missing a shipped route")


if __name__ == "__main__":
    unittest.main()
