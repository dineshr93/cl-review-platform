# Attack step 2 of the scout's order (C1, C2, E1, E2) — make CI honest

Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md` (findings C1, C2, E1, E2 carry the file:line, proposed fix and cost). Fix them in this order.

- **C1** `.github/workflows/ci.yml:31` runs `python -m unittest discover -s tests -t tests`, which never collects `tests/test_db_concurrency.py:9` — a bare module-level `def test_...` with no `TestCase`. Verified: unittest runs 102 tests, pytest runs 103; the missing one guards `clreview/db.py:141-166`. Either wrap it in a `unittest.TestCase` or make CI run pytest (C2 does that) — the concurrency test must actually execute.
- **C2** two runners disagree: `Makefile:11` runs pytest, CI runs unittest, and `pyproject.toml` has `[tool.pytest.ini_options]` but never declares pytest, so `pip install -e . && pytest` fails on a fresh clone. Declare a dev dependency group (pytest, pytest-cov, ruff), add a coverage gate to `addopts`, and make CI and the Makefile run the same runner. Pick the `--cov-fail-under` floor from the coverage you actually measure, at or just below it, and state the number.
- **E1** CI has no lint, no typecheck, no coverage. `.gitignore:8` already lists `.ruff_cache/`, so ruff was intended and never configured. Add `ruff check clreview tests` to CI and a ruff config in `pyproject.toml` that passes today — start permissive rather than renaming code.
- **E2** `.github/workflows/release.yml` builds and publishes on any `v*` tag without running the suite, and uploads `dist/SHA256SUMS.txt` that nothing verifies. Make the release depend on a test run before build, add build-provenance attestation, and verify the checksums in the verify step.

Where: `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `pyproject.toml`, `Makefile`, `tests/test_db_concurrency.py`
Done means: `uv run pytest -q --ignore=adws` is green with the coverage gate enabled and the concurrency test collected (its name appears in the verbose collection); `ruff check clreview tests` exits 0; both workflow files parse as YAML; the unittest-vs-pytest collected-count gap is closed.
Out of scope: findings A5–A13, B, D, F, and any C finding other than C1/C2; no runtime dependency additions; no test-suite rewrites beyond making C1 collectable; no repo settings or branch-protection changes.
