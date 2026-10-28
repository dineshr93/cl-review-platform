# Attack step 9 — test tail (C5, C7, C8)

Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`. Each finding below carries its file:line, evidence, proposed fix, cost and risk there — read them and work them in this order. C1, C2, C3, C4 and C6 already landed; do not redo them.

- **C5** ~100 `raise` sites, 22 `assertRaises` — most failure paths are untested.
- **C7** missing negative/edge scenarios.
- **C8** test fixtures leak temp dirs and are never cleaned.

Where: `tests/` (all of it), `tests/helpers.py`, and `clreview/` only where a test exposes a real defect — name it in the plan rather than silently changing behaviour
Done means: `uv run pytest -q --ignore=adws` is green with the coverage gate passing and `ruff check clreview tests` exits 0; C5 and C7 are closed by tests that fail without the code they pin, chosen for the raises that matter (connector failures, validation rejections, permission gates) rather than a sweep for coverage's sake; C8 is closed by fixtures that clean up after themselves — prove it by running the suite twice in a row and showing no temp dirs survive (`ls /tmp/clreview-test-*` empty afterwards); report the new total coverage and the new test count.
Out of scope: A, B, D, E, F findings; C1–C4, C6 (done); property-based or fuzzing frameworks; new test dependencies beyond what is already declared; tests that only restate an implementation detail.
