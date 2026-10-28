# Attack step 6 — correctness tail (A5, A6, A7, A8, A9, A10, A11, A12)

Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`. Each finding below carries its file:line, proposed fix, cost and risk there — read them and fix them in this order. A13 already landed in commit `2d80668`; do not redo it.

- **A5** Non-idempotent POSTs are retried in Black Duck.
- **A6** Silent pagination truncation in a compliance tool.
- **A7** ScanCode license-expression parser invents entries.
- **A8** Black Duck license resolution scans only the first 100 KB hits.
- **A9** Contract leaks: raw exceptions escape `ConnectorError`.
- **A10** No transaction around multi-statement writes.
- **A11** 404 bodies carry Python `KeyError` repr.
- **A12** Body handling: absent Content-Length silently means `{}`.

Where: `clreview/connectors/blackduck.py`, `clreview/connectors/scancode.py`, `clreview/connectors/http.py`, `clreview/connectors/base.py`, `clreview/service.py`, `clreview/api.py`, `clreview/db.py`, and the matching tests (`tests/test_connectors.py`, `tests/test_e2e.py`, `tests/test_review.py`, `tests/test_db_schema.py`)
Done means: `uv run pytest -q --ignore=adws` is green with the coverage gate passing and `ruff check clreview tests` exits 0; each finding is pinned by a test that fails without its fix — a retry test asserting a POST is not retried and a PUT is, a test that a truncated page is reported rather than silently dropped, a parser test on the expression A7 mis-parses, a test that license resolution does not stop at the first 100 KB, a test that connector failures surface as `ConnectorError`, a test that a partially failed multi-statement write leaves no half-written rows, a test asserting the 404 body is a clean message and not a `KeyError` repr, and a test that a bodyless request is rejected rather than treated as `{}`. Report the new total coverage.
Out of scope: B, C, D, E, F findings; A13 (done); API contract changes beyond what these fixes force; new dependencies; refactors of code these findings do not name.
