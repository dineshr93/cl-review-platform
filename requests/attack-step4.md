# Attack step 4 of the scout's order (B1) — make `refresh()` matching scale

Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`, finding B1 (file:line, measurements, three fixes in order of value, cost and risk).

`_run_matching` is superlinear. Scout's verified numbers on this machine, identical PL/SL, `/tmp` DB: N=200 → 0.1 s, N=800 → 2.0 s, N=1600 → 4.2 s. cProfile at N=1600: 20,804 `db.query` calls = 3.80 s of 4.18 s; `component_observations` 6,400 calls; `_inventory_for_matching` 2.50 s of 4.18 s. Two causes: `_inventory_for_matching` (`clreview/service.py:246-259`) re-reads every component plus its three observation tables (2 × N × 4 queries) to rebuild `ComponentData` that was just written; and `recompute()` (`clreview/review.py:101-127`) runs once per match, each re-running `comparison()` → `component_by_key` → `current_snapshot` + 3 observation queries. Under the global `DB.lock` (`clreview/db.py:141`) this blocks every HTTP request.

Apply the scout's three fixes in its stated order of value:
1. Batch-load observations per snapshot — one `SELECT ... WHERE component_id IN (...)` per table instead of per component.
2. In `_run_matching`, recompute only matches whose row changed (new insert, or updated names/versions), not all.
3. Cache `current_snapshot(mapping_id, side)` per call frame — it is re-queried on every `component_by_key`.

Where: `clreview/service.py`, `clreview/review.py`, `clreview/matching.py`, `clreview/comparison.py`, `clreview/db.py` (read-only unless a batch query helper is genuinely needed), `tests/test_matching.py`, `tests/test_comparison.py`, `tests/test_sync.py`, `tests/test_review.py`
Done means: `uv run pytest -q --ignore=adws` is green with the coverage gate passing; a new scale test builds an N=1600 identical-PL/SL inventory in a temp DB and asserts the query count of `_run_matching` has fallen from the ~20,804 the scout measured to something bounded per table (assert on query count or call count, not wall-clock, so CI is not flaky); the plan states the measured before/after seconds for N=200/800/1600 on this machine; every existing matching, comparison and recompute test still passes unchanged, so semantics did not move.
Out of scope: B2–B9 (indexes and FKs already landed), every A/C/D/E/F finding, HTTP/API surface changes, concurrency or locking changes in `clreview/db.py:141-166`, caching layers that outlive a single call frame, dependency additions.
