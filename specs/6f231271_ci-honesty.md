# Plan — attack step 2: C1, C2, E1, E2 (make CI honest)

Repo: `/home/dinesh/repos/foss_data_reviewer`, on top of `7fd997a` (attack step 1 landed). Work the findings in the order **C1 → C2 → E1 → E2**; each is one commit. Everything below was measured on this checkout, not assumed.

## Baseline (measured before any edit)

```
uv run pytest -q --ignore=adws            -> 108 passed (~40 s)
uv run python -m unittest discover -s tests -t tests -> Ran 107 tests, OK
uv run pytest --collect-only -q --no-cov --ignore=adws | grep -c "::"   -> 108
uv run python -c "import unittest; print(unittest.TestLoader().discover('tests', top_level_dir='tests').countTestCases())" -> 107
coverage (uv run --with pytest-cov pytest --cov=clreview) -> TOTAL 2694 stmts, 630 miss, 76.61%
ruff 0.17.3, no config anywhere in the repo:
  ruff check clreview tests                       -> 53 errors   (0.17 default rule set is wider than E4,E7,E9,F)
  ruff check --isolated --select E4,E7,E9,F ...   -> 18 errors   (13 F401 all safe-fixable, 4 E741, 1 E731)
uv.lock -> 139-byte stub: locks only the editable self, no dev deps
```
The 1-test gap is exactly `tests/test_db_concurrency.py:9` — a module-level `def test_...` with no `TestCase`, so `unittest` never collects it while pytest does.

**Coverage floor: `--cov-fail-under=75`.** Measured 76.61%; 75 sits just below it with ~1.6 points of headroom. Verified the gate bites: with `--cov-fail-under=99` the run exits 1, with 75 it prints `Required test coverage of 75% reached. Total coverage: 76.61%` and exits 0.

**Runner decision (C2):** one runner everywhere — **`uv run pytest`**. The repo is already uv-native (`Makefile` builds the venv with `uv venv`, every local command in this session is `uv run …`), the "Done means" command is `uv run pytest`, and `[dependency-groups]` (PEP 735) is what `uv run` auto-installs, so `uv run pytest` works with no extra flags. pip cannot see `[dependency-groups]` with `pip install -e .`, so CI moves to `uv` rather than duplicating the dev list in YAML. A pip fallback is given at the end if uv misbehaves in CI.

---

## C1 — make the concurrency test collectable under both runners

**File:** `tests/test_db_concurrency.py` — wrap the module-level function in a `unittest.TestCase`. Body unchanged except bare `assert` → `self.assert*` (same semantics; the worker's `except BaseException` still catches `AssertionError`, so failures are still collected and reported with the cause).

```python
"""The HTTP server is threaded and hands every request the same sqlite connection."""
from __future__ import annotations

import threading
import unittest

from clreview.db import DB


class DbConcurrencyTests(unittest.TestCase):
    def test_concurrent_statements_are_serialized(self):
        db = DB(":memory:")
        errors: list[BaseException] = []

        def worker(actor: str) -> None:
            try:
                for i in range(40):
                    db.execute("INSERT INTO audit(at,actor,action) VALUES('now',?,?)", (actor, f"e{i}"))
                    row = db.one("SELECT action FROM audit WHERE actor=? ORDER BY id DESC LIMIT 1", (actor,))
                    self.assertTrue(row and row["action"] == f"e{i}")
                    db.query("SELECT id FROM audit LIMIT 10")
            except BaseException as exc:  # collected so the assertion below names the cause
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, f"concurrent sqlite access raised: {errors[:3]}")
        self.assertEqual(len(db.query("SELECT id FROM audit")), 8 * 40)
```
Verified: `unittest` collection goes 107 → **108**, matching pytest's 108; the test passes in 0.01 s and `ruff check` is clean on it. Do not touch `clreview/db.py:141-166` — this test is the guard on that lock.

---

## C2 — one runner, declared dev dependencies, coverage gate

**File:** `pyproject.toml` — add the dev group and the gate:

```toml
[dependency-groups]
dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.9"]

[tool.pytest.ini_options]
testpaths = ["tests"]   # keep `make test` from collecting adws/adw_*_test.py (SSSF scripts, not tests)
addopts = "--cov=clreview --cov-report=term-missing --cov-fail-under=75"
```
Keep the existing `testpaths` comment. No runtime dependency changes.

Then **run `uv lock` and commit the regenerated `uv.lock`** — it goes from the 139-byte stub to a real lock (~490 lines) carrying pytest, pytest-cov and ruff, which is what makes `uv sync` in CI reproducible. Verified: `uv lock` succeeds and `uv run pytest -q --ignore=adws` then installs the dev group automatically and exits 0 with no extra flags.

**File:** `Makefile` — same runner as CI, and drop the now-unused `VENV` variable (deletion before addition):

```make
.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?##' Makefile | awk -F':.*?##' '{printf "%-8s %s\n", $$1, $$2}'

.venv:
	uv sync

test: .venv ## run pytest with the coverage gate (same command as CI)
	uv run pytest -q --ignore=adws

lint: ## ruff check (same command as CI)
	uv run ruff check clreview tests

demo: .venv ## seed mock PL/SL data and serve UI on :8765
	uv run python -m clreview.server --demo
```
(`PORT ?= 8765` and the `stop:` target are unchanged.)

**Consequence to accept, not fix:** with `--cov` in `addopts`, a single-file run (`uv run pytest -q tests/test_matching.py`) exits 1 because one file's tests cannot reach 75%. The escape hatch is `--no-cov` (`uv run pytest -q --no-cov tests/test_matching.py` → 7 passed). `--collect-only` is unaffected (exits 0).

**Optional, 2 lines, in scope because C2 is about which runner people use:** `README.md:46` and `README.md:260` still tell readers to run `python3 -m unittest discover -s tests -t tests`. Replace those two commands with `uv run pytest -q --ignore=adws`. Do **not** touch the test counts in README/PRODUCT/CHANGELOG — that is finding F1, out of scope.

---

## E1 — ruff config that passes today, plus a lint step in CI

**File:** `pyproject.toml` — add:

```toml
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F"]     # pycodestyle errors + pyflakes: real defects, not style churn
ignore = ["E741", "E731"]            # ambiguous name `l`, lambda assignment: renaming is out of scope
```
`select` must be explicit: ruff 0.17's built-in default is wider than the historical `E4,E7,E9,F` set and reports 53 errors here (I001 import sorting, RUF015, PLW1510, …). Pinning the set also makes the result independent of any ambient ruff config.

**Then clear the 13 F401s — one command, no renaming:**

```bash
uv run ruff check --fix clreview tests   # 13 unused imports removed, "No issues found"
```
Verified end to end: after `--fix`, `ruff check clreview tests` exits 0 and the suite is still green (107 passed with the smoke deselected, coverage 76.60%). The whole diff is import lines:

| file | removed |
|---|---|
| `clreview/api.py` | `sys` |
| `clreview/connectors/blackduck.py` | `UnsupportedOperation` (from `.http`) |
| `clreview/connectors/fossology.py` | `get_json` (from `.http`) |
| `clreview/review.py` | `IRRELEVANT`, `REJECT`, `FALSE_POSITIVE`, `sha256` (from `.domain`) |
| `clreview/service.py` | `hashlib` |
| `clreview/sync.py` | `APPROVE_SL`, `CORRECT` |
| `tests/test_e2e.py` | `clreview.service.Service` |
| `tests/test_review.py` | `helpers.fixture_set` |
| `tests/test_sync.py` | `clreview.connectors.base.ConnectorError` |

Sanity-check before committing: `grep -rn "review\.REJECT\|review\.IRRELEVANT\|review\.sha256" clreview tests` must be empty (nothing re-exports those names through `review.py`), and the suite must stay green. Do **not** fix E741/E731 by renaming `l` — that is why they are in `ignore`.

**File:** `.github/workflows/ci.yml` — full replacement (test job moves to uv + pytest, new lint job, `package` job untouched):

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    name: lint (ruff)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.13"
      - name: Lint
        run: uv run ruff check clreview tests

  test:
    name: test (py${{ matrix.python }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python: ["3.10", "3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: ${{ matrix.python }}
      - name: Install
        run: uv sync
      - name: Byte-compile
        run: uv run python -m compileall -q clreview
      - name: Tests
        run: uv run pytest -q --ignore=adws

  package:
    name: package
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Build sdist and wheel
        run: python -m pip install --upgrade pip build && python -m build
      - name: Verify wheel contents and entry point
        run: |
          python -m pip install dist/*.whl
          clreview-server --version
          python - <<'PY'
          import importlib.resources as r
          assert r.files("clreview").joinpath("web/index.html").is_file()
          assert r.files("clreview").joinpath("fixtures/demo/pl.json").is_file()
          print("wheel assets ok")
          PY
      - name: Smoke-run packaged server
        run: |
          clreview-server --demo --port 8899 --db /tmp/smoke.db >/tmp/server.log 2>&1 &
          for i in $(seq 1 30); do
            code=$(curl -s -o /tmp/health.json -w '%{http_code}' http://127.0.0.1:8899/api/health || true)
            [ "$code" = "200" ] && break
            sleep 0.3
          done
          test "$code" = "200"
          curl -s -o /tmp/index.html -w '%{http_code}' http://127.0.0.1:8899/ | grep -q 200
          kill %1 2>/dev/null || true
```
The exact YAML above was parsed with PyYAML before planning (`jobs = ['lint','package','test']`). `enable-cache` on setup-uv is finding E5 — leave it out.

---

## E2 — release runs the suite first, attests provenance, verifies checksums

**File:** `.github/workflows/release.yml` — three changes: a `test` job the release `needs`, `attestations`/`id-token` permissions plus an attestation step, and `sha256sum -c` in the verify step. The build and `gh release` steps stay byte-identical — they work; do not "improve" them.

```yaml
name: Release

on:
  push:
    tags: ["v*"]
  workflow_dispatch:
    inputs:
      tag:
        description: "Existing tag to release (for example v1.0.0)"
        required: true
        type: string

permissions:
  contents: write
  attestations: write
  id-token: write

concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false

jobs:
  test:
    name: test the tag before publishing
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.13"
      - name: Install
        run: uv sync
      - name: Lint
        run: uv run ruff check clreview tests
      - name: Tests
        run: uv run pytest -q --ignore=adws

  release:
    needs: test
    runs-on: ubuntu-latest
    env:
      TAG: ${{ inputs.tag || github.ref_name }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Build artifacts
        run: |
          python -m pip install --upgrade pip build
          python -m build
          (cd dist && sha256sum * > SHA256SUMS.txt)

      - name: Verify artifacts
        run: |
          (cd dist && sha256sum -c SHA256SUMS.txt)
          python -m pip install dist/*.whl
          clreview-server --version
          python - <<'PY'
          import importlib.resources as r
          assert r.files("clreview").joinpath("web/index.html").is_file()
          assert r.files("clreview").joinpath("fixtures/demo/pl.json").is_file()
          print("artifacts ok")
          PY

      - name: Attest build provenance
        uses: actions/attest-build-provenance@v2
        with:
          subject-path: dist/*

      - name: Create or update GitHub release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          if gh release view "$TAG" >/dev/null 2>&1; then
            gh release upload "$TAG" dist/* --clobber
          else
            gh release create "$TAG" dist/* \
              --title "$TAG" \
              --generate-notes \
              --verify-tag
          fi
```
Notes: `sha256sum *` runs before `SHA256SUMS.txt` exists, so the file does not contain itself and `sha256sum -c` verifies cleanly. `actions/attest-build-provenance@v2` needs the two new permissions; if the repo disallows attestations the step fails loudly rather than publishing unattested artifacts — that is the intended behaviour, and no repo-settings change is in scope. The `test` job checks out the tag commit automatically on a tag push. Verified: this file parses (`jobs = ['release','test']`, `release.needs = test`, permissions carry `attestations: write` and `id-token: write`).

---

## Files touched

| file | change |
|---|---|
| `tests/test_db_concurrency.py` | C1: wrap in `unittest.TestCase` |
| `pyproject.toml` | C2: `[dependency-groups] dev`, `addopts` coverage gate; E1: `[tool.ruff]` + `[tool.ruff.lint]` |
| `uv.lock` | C2: regenerated by `uv lock` (stub → real lock with dev deps) |
| `Makefile` | C2: `uv sync` / `uv run pytest` / new `lint` target; drop unused `VENV` |
| `.github/workflows/ci.yml` | C2 + E1: uv install, pytest with the gate, new `lint` job |
| `.github/workflows/release.yml` | E2: `test` job + `needs`, provenance attestation, `sha256sum -c` |
| source files (13 import lines) | E1: `uv run ruff check --fix` output only |
| `README.md` (optional, 2 lines) | C2: point the two documented commands at `uv run pytest -q --ignore=adws`; leave the test counts alone (F1) |

## Verification — all of these must hold

```bash
uv run pytest -q --ignore=adws
#   expect: "Required test coverage of 75% reached. Total coverage: 76.6%", "108 passed", exit 0
uv run pytest --collect-only -q --no-cov --ignore=adws | grep -c "::"          # 108
uv run pytest --collect-only -q --no-cov --ignore=adws | grep concurrent
#   expect: tests/test_db_concurrency.py::DbConcurrencyTests::test_concurrent_statements_are_serialized
uv run python -c "import unittest; print(unittest.TestLoader().discover('tests', top_level_dir='tests').countTestCases())"   # 108 == pytest's 108
uv run ruff check clreview tests                                               # exit 0, "No issues found"
make test && make lint                                                          # same commands CI runs
uv run --with pyyaml python -c "import yaml; [yaml.safe_load(open(f)) for f in ['.github/workflows/ci.yml','.github/workflows/release.yml']]; print('yaml ok')"
git diff --stat                                                                  # eyeball: no runtime dep change, no workflow secrets, no scope creep
```
The browser smoke (`tests/test_web_smoke.py`) skips in CI (no chromium on `ubuntu-latest`, finding C6 — out of scope). Locally it runs and passes **only because `clreview.db` exists in the repo root** — the smoke spawns `python -m clreview.server --demo` with no `--db`, so it reads whatever DB file is already there. If it fails after your change, confirm it also fails at `git stash`ed HEAD before believing you broke something.

**Fallback if uv misbehaves in CI:** keep the pip matrix and just switch the runner — `python -m pip install --upgrade pip && python -m pip install -e . pytest pytest-cov ruff`, then `python -m ruff check clreview tests` and `python -m pytest -q --ignore=adws`. Same runner, same gate; the dev list is then duplicated in YAML, which is what `[dependency-groups]` avoids.

## Out of scope

A5–A13, B, D, F (including the wrong test counts in README/PRODUCT/CHANGELOG — F1), and every C finding other than C1/C2 (C3–C9). Also out: E3–E7 (docker build in CI, uv/pip caching, `.dockerignore`, action SHA pinning), typecheckers (mypy/pyright), coverage config beyond the floor, branch protection or any repo settings, test-suite rewrites beyond making the C1 test collectable, and any runtime dependency addition.
