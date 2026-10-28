# Attack step 7 — performance tail (B4, B5, B6, B7, B8, B9)

Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`. Each finding below carries its file:line, measurements, proposed fix, cost and risk there — read them and fix them in this order. B1 (matching scale), B2 (indexes) and B3 (foreign keys) already landed in `b7fae62` and `73a63a9`; do not redo them.

- **B4** N+1 query fan-out in read endpoints.
- **B5** Unbounded snapshot/match growth.
- **B6** Black Duck N+1 HTTP: origins/copyrights uncached.
- **B7** FOSSology tree walk is one GET per directory, serial.
- **B8** Per-statement commit in `DB.execute`.
- **B9** Black Duck POSTs one copyright notice at a time.

Where: `clreview/db.py`, `clreview/service.py`, `clreview/api.py`, `clreview/sync.py`, `clreview/connectors/blackduck.py`, `clreview/connectors/fossology.py`, `clreview/connectors/http.py`, and the matching tests
Done means: `uv run pytest -q --ignore=adws` is green with the coverage gate passing and `ruff check clreview tests` exits 0; each fix is pinned by a test that fails without it — a read endpoint asserted to issue a bounded statement count instead of one per row, a growth bound asserted for snapshots/matches, a connector test with a counting fake transport asserting origins/copyrights are fetched once not per component, a tree-walk test asserting the request count scales with directories not files, a test that a multi-statement write commits once, and a test that copyright notices go out in one request. Where a fix changes measured cost, state the before/after count in the plan the way B1's plan did. Report the new total coverage.
Out of scope: A, C, D, E, F findings; B1/B2/B3 (done); any caching layer that outlives a single call frame; HTTP/2, connection pooling or thread-pool changes; dependency additions.
