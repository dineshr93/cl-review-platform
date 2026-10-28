# Step 7 — Performance tail: B4, B5, B6, B7, B8, B9

Session `6f231271`. Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`.
HEAD at planning time: `455515d` (step 5). B1/B2/B3 are done (`b7fae62`, `73a63a9`) — do not redo them.

Everything below was **measured on this machine** in a scratch copy of HEAD (`/tmp/s7head` baseline,
`/tmp/s7` fixed). Baseline suite: **183 passed, coverage 85.15%**. After the work:
**208 passed, coverage 86.36%**, ruff clean, `unittest discover` collects the same 208.

---

## 0. Ground rules

* Runner is `uv`. Verification is `uv run pytest -q --ignore=adws` (coverage gate `--cov-fail-under=75`)
  and `uv run ruff check clreview tests`.
* Single-file pytest runs need `--no-cov` (the `--cov` flags live in `addopts`).
* No new dependencies. No HTTP/2, no connection pooling, no thread-pool changes, no caching that
  outlives one call frame. A, C, D, E, F findings and B1/B2/B3 are out of scope.
* Every fix is pinned by a test that **fails without it** (red proof in §7).
* Cost is asserted as **statement counts, row counts, request counts and commits** — never
  wall-clock. Milliseconds fail on a loaded CI box; a statement count says exactly what changed.

---

## 1. Measured baseline (before)

All numbers from `/tmp/bench_b4.py`, `/tmp/bench_b6b7.py`, `/tmp/bench_b8.py`, `/tmp/bench_bytes.py`
run against a pristine HEAD.

### B4 — read fan-out

| read | N=50 | N=200 | N=800 |
|---|---|---|---|
| `POST /api/sync/preview` | 705 stmts / 752 rows / 3.0 ms | 2,805 stmts / 3,002 rows / 12.5 ms | **11,205 stmts / 12,002 rows / 79.9 ms** |
| `review.list_components` (no `q`) | 11 stmts / 354 rows | 11 stmts / 1,404 rows | 11 stmts / 5,604 rows |
| `review.list_components` (`q=`) | 11 stmts / 354 rows | 11 stmts / 1,404 rows | 11 stmts / 5,604 rows |
| `list_connections` + `list_mappings` (10 mappings) | — | — | 34 stmts / 44 rows |
| `sync.list_jobs` (5 jobs, items each) | — | — | 111 stmts / 210 rows |

Bytes pulled out of SQLite by one queue request: **189,944 @ N=200**, **768,945 @ N=800**.

Root causes:

* `clreview/api.py:144` `/api/sync/preview` — per match: one `SELECT * FROM decisions WHERE
  match_id=?`, plus `preview_match` → `s.mapping()`, `connector_for()`, `_pl_observations()`,
  `_pl_component()`, `component_by_key()`. ~14 statements per component.
* `clreview/review.py:150` `list_components` — builds license-name and identifier maps for the
  **whole snapshot of both sides** on every request, then filters, sorts and paginates in Python.
* `clreview/service.py:457` `_current_matches` (used by the queue, overview, preview, `create_job`)
  calls `snapshot_components()` for both sides, i.e. `SELECT *` including `metadata_json` and
  `raw_json`, to build a set of external ids.
* `clreview/service.py` `list_connections` / `list_mappings` — one query per row, nested.
* `clreview/sync.py` `list_jobs` — `job()` per row, and `job()` does one `SELECT … FROM matches`
  per sync item.

### B5 — unbounded growth

9 refreshes of a 40-component mapping: **snapshots 18, components 720, matches 40**. Every refresh
copies the whole inventory again and nothing ever prunes. `_current_matches` hides stale matches at
read time but never deletes them, so dead rows accumulate forever too.

### B6 — Black Duck N+1 HTTP

`fetch_inventory` of 60 BOM components sharing one origin: **121 HTTP requests** (1 listing + 60
origin detail + 60 copyright collections). `_license_cache` / `_text_cache` memoize licenses;
origins and copyright collections are not memoized at all.

### B7 — FOSSology tree walk

One upload, 20 noise dirs × 20 noise files + 1 wanted license file: **33 requests, 23 tree walks**.
With 3× the noise dirs: **81 requests, 63 tree walks** — cost is proportional to the whole tree,
although the walk exists only to find a handful of license files.

### B8 — commit per statement

`DB.execute` commits after every statement outside a transaction. Measured per call:
`refresh()` **246 commits**, `create_job()` 4, a single `decide()` 3, `add_note()` 2.

### B9 — one copyright POST per notice

`update_copyrights` with 2 notices (1 already present): **5 requests**
(`GET bom`, `GET origin`, `GET origin`, `GET copyrights`, `POST copyrights`) — the origin is
resolved twice, and each new notice is POSTed on its own.

---

## 2. B4 — stop the read fan-out

**Files:** `clreview/api.py`, `clreview/service.py`, `clreview/review.py`, `clreview/sync.py`.

### 2.1 `PreviewContext` (`clreview/sync.py`)

Add a class that reads everything a whole preview needs, once:

```python
class PreviewContext:
    def __init__(self, s: Service, mapping_id: int):
        self.mapping = s.mapping(mapping_id)
        self.caps = s.connector_for(self.mapping["pl_connection_id"]).capabilities
        self.sides = {side: s.side_components_with_observations(mapping_id, side)
                      for side in ("pl", "sl")}

    def source_version(self, side, key) -> str: ...      # '' when the key is unknown
    def observations(self, side, key) -> dict: ...       # copyrights / licenses / license_texts
```

`preview_match(s, match, decisions, ctx=None)` takes an optional context and falls back to building
one, so every existing caller (`review.detail`, `create_job`, tests) keeps working unchanged.
`_pl_component` stays (the write-verification path uses it); `_pl_observations` loses its last
caller and is **deleted** - do not leave it behind.

`SyncService.create_job` builds one context and one batched `decisions_by_match` read for the whole
approved queue instead of a `SELECT * FROM decisions` per match.

`clreview/api.py` `/api/sync/preview` does the same: `decisions_by_match(self.db, ids)` +
one `PreviewContext`, then `preview_match(service, match, decided.get(id, []), ctx)`.

`side_components_with_observations` already exists from B1 — reuse it, do not write a second
bulk loader.

### 2.2 Page-scoped queue maps (`clreview/review.py`)

Reorder `list_components` so the expensive maps are built after the page is known:

1. cheap filters (match status, review status, confidence) — needs only the match row,
2. sort — every sort key uses only match-row fields,
3. if `q` is set: build the full both-side maps (`_side_maps(mapping_id)`) and filter — a text
   search genuinely has to look at everything,
4. slice the page,
5. if `q` was not set: build the maps **for the page's keys only** (`_side_maps(mapping_id, page)`),
6. build the rows with `_queue_row`.

Extract `_side_maps(mapping_id, page=None)`, `_haystack(match, lic_names, ident)` and
`_queue_row(match, lic_names, ident)` so the search path and the page path share one definition of
"what a row contains" — the refactor must not change a single response field.

`_side_maps` adds `AND external_id IN (…)` to both per-side queries when a page is given.

### 2.3 Cheap presence read (`clreview/service.py`)

Add `Service.snapshot_component_keys(snapshot_id) -> set[str]` (`SELECT external_id FROM components
WHERE snapshot_id=?`) and use it in `_current_matches` instead of `snapshot_components()` for both
sides. `snapshot_components()` stays for the two callers that really need names and versions
(`_inventory_for_matching`, the reassign candidate list in `review.detail`).

### 2.4 Flat listings (`clreview/service.py`, `clreview/sync.py`)

* `Service._shape_connection(row)` — the JSON/sanitising tail of `connection()`, so
  `list_connections` is one `SELECT *` instead of one `SELECT` per row.
* `Service._shape_mapping(row, connections)` — `list_mappings` reads all connections once and all
  mappings once. `mapping(id)` keeps its signature (it now reuses `list_connections`; 2 statements
  instead of 3).
* `SyncService._jobs(job_ids)` — three reads for any number of jobs (`sync_jobs IN`, `sync_items
  IN`, `matches IN` chunked through `_chunks`), grouped per job in Python. `job(id)` is
  `_jobs([id])[0]` and still raises `KeyError`; `list_jobs` keeps its order and its response shape
  (`items`, `components[].counts`, parsed `pl_before`, `summary`, `dry_run`).

---

## 3. B5 — bound the history

**File:** `clreview/service.py`.

`SNAPSHOTS_PER_SIDE = 5` and two new methods:

* `prune_snapshots(mapping_id, keep=SNAPSHOTS_PER_SIDE)` — per side, delete every snapshot whose id
  is not in the newest `keep` (`id NOT IN (SELECT id … ORDER BY id DESC LIMIT ?)`); the FK cascade
  from B3 removes their components, copyrights, licenses and license texts. Called at the end of
  `refresh()`, after matching, and reported as `matching["snapshots_pruned"]`.
* `prune_stale_matches(mapping_id, pl_snapshot_id, sl_snapshot_id)` — delete match rows for
  components that no longer exist on **either** side, and only rows nobody touched:
  `status != 'confirmed'`, `review_status = 'not_reviewed'`, empty `review_state`, and no row in
  `decisions`, `notes` or `suggestions`. Called from `_run_matching` inside the same transaction as
  the match writes; reported as `matching["matches_pruned"]`.

Why `keep = 5`: incremental matching (B1) needs the previous snapshot, and a reviewer's "what did I
just see" is recent history. Decisions, notes and suggestions are never deleted — reviewer work
outlives the snapshot it was made against, and `decisions` has no FK to `snapshots`.

A match that is merely **stale** (one side still present) is kept: a stale match is a finding, not
noise. Only rows with nothing on either side are garbage.

---

## 4. B6 + B9 — Black Duck round trips

**File:** `clreview/connectors/blackduck.py`.

* `_new_http_cache()` returns `{"origins": {}, "collections": {}}`. `fetch_inventory` creates one
  cache and passes it to every `_copyrights(raw, cache)` call; `_copyright_collections(raw, cache)`
  memoizes the origin detail GET and `_copyrights` memoizes the paginated collection items per href.
* The cache is **a parameter, created per call** — it must not live on the connector. A write
  followed by a verification read has to see the write (`_execute` verifies right after writing),
  so a `self.`-level memo would be a correctness bug. Pinned by
  `test_a_second_inventory_read_asks_the_network_again`.
* `update_copyrights` builds one cache and passes it to both `_copyright_collections` and
  `_copyrights`, which removes the duplicate origin GET.

**B9 deviation, deliberate:** the scout suggested one bulk request for the notices. Black Duck's
documented bulk `PUT …/copyrights` ("Updating Multiple Copyrights", REST reference
`apis/blackduck_Blackduck-REST-API-v2026.4.2.md`) updates **existing** statements — every object in
the body must carry an `_meta.href`, which a not-yet-existing copyright does not have. There is no
documented single-request create for N new notices, so `update_copyrights` keeps one `POST` per new
notice and says so in its docstring. Do not invent an undocumented request body. The measurable win
that *is* documented is dropping the duplicate reads: 5 requests → 4 for the measured case.

---

## 5. B7 — FOSSology tree walk

**File:** `clreview/connectors/fossology.py`.

* Replace the recursive `_walk` with a breadth-first loop inside `_item_map(scan_id, wanted=None)`
  (`from collections import deque`). Keep the depth cap (40) and the 50,000-entry cap.
* After each directory: `if wanted and wanted <= out.keys(): break`.
* `_upload_inventory` computes `wanted` up front — the license file paths it is actually going to
  fetch, `is_license_file(p)[: self.max_license_files]` per group — and calls
  `_item_map(scan_id, wanted)` once, before the loop. `item_map` is no longer lazily built inside
  the loop.
* Cache rule: a cached full map is returned for any `wanted` (a superset answers every question);
  a walk that **stopped early is not cached**, because handing a partial map to a later caller would
  make it report real files as missing. `self._item_maps` keeps that role.

`_walk` disappears; nothing else called it.

---

## 6. B8 — one commit per write

**Files:** `clreview/db.py`, `clreview/review.py`, `clreview/service.py`, `clreview/sync.py`.

* `DB.commits` counter, incremented wherever a commit actually happens (the `execute` fast path and
  the outermost `transaction()` exit). It exists so the tests can assert commit counts instead of
  guessing from timing.
* Wrap every remaining multi-statement write in the existing `DB.transaction()` helper (added in
  step 6 / A10 — do not add a second transaction mechanism):
  * `Service`: `add_connection`, `create_mapping`, both branches of `test_connection`.
  * `ReviewService`: `decide` (decision upsert + audit + recompute), `add_suggestion`,
    `add_note`, `set_review_state`, `confirm_match`, `reassign`, `reject_match`, and the
    `decide_suggestion` reject branch.
  * `Service._run_matching`: the whole write phase (match UPDATEs/INSERTs + recompute + the new
    match prune) is one transaction.
  * `SyncService._execute`: the per-item result UPDATE and its audit row.
  * `SyncService.retry_job`: new job row + item rows + audit row.
* Network calls stay outside every transaction: `refresh()`'s `fetch_inventory()`,
  `sync._execute()`'s connector writes, `test_connection`'s probe. The write lock is held for the
  block, so a transaction must never span a round trip.
* Already wrapped in step 6, unchanged: `_store_snapshot`, `decide_suggestion` accept path,
  `create_job` creation.

---

## 7. Tests

New file **`tests/test_read_scale.py`** (20 tests, `unittest.TestCase` so `unittest discover`
collects them too). It counts statements/rows by wrapping `db.query` / `db.one` / `db.execute`
in a `Counter` context manager, and commits through `db.commits`.

| class | test | pins |
|---|---|---|
| `PreviewFanoutTests` | `test_preview_statements_do_not_grow_with_the_queue` | B4 preview: 40 → 400 components costs ≤ 8 more statements |
| | `test_preview_still_reports_the_approved_changes` | the batched read returns the same components/items |
| `QueueListingTests` | `test_the_page_reads_only_its_own_components` | B4 queue: license rows ≤ 2×page, page-scoped component rows ≤ 2×page, **zero** `SELECT * FROM components` reads, statements flat |
| | `test_search_still_reaches_beyond_the_page` | `q=` by name, by license and by purl still finds rows outside the page |
| | `test_row_content_survives_the_page_scoped_read` | a queue row still carries licenses/method/review_status/mismatch_reasons |
| `ConnectionListingTests` | `test_listing_mappings_reads_connections_once` | B4: 10 mappings in ≤ 4 statements |
| `JobListingTests` | `test_list_jobs_reads_scale_with_jobs_not_items` | B4: job listing cost does not follow item count |
| | `test_job_content_is_unchanged_by_the_batched_read` | grouped components, counts, `pl_before`, `job(id) == list_jobs()[0]` |
| `HistoryGrowthTests` | `test_refresh_stops_copying_history_forever` | B5: after 9 refreshes, snapshots = 2×`SNAPSHOTS_PER_SIDE`, components = 2×keep×N |
| | `test_the_previous_snapshot_survives_the_prune` | incremental matching still has a previous snapshot |
| | `test_reviewer_history_outlives_the_prune` | a decision survives 9 refreshes and still drives the comparison |
| | `test_matches_for_components_that_vanished_are_dropped` | B5: 5 vanished components → `matches_pruned == 5`, decided row kept |
| | `test_a_stale_match_is_kept_because_it_is_a_finding` | one-sided loss prunes nothing |
| `CommitDisciplineTests` | `test_a_decision_is_one_commit`, `test_review_state_and_recompute_are_one_commit`, `test_a_note_and_its_audit_row_are_one_commit`, `test_confirmation_is_one_commit`, `test_a_suggestion_and_its_audit_row_are_one_commit`, `test_a_sync_job_is_one_commit` | B8: exactly 1 commit each |
| | `test_a_refresh_commits_its_phases_not_its_rows` | B8: a 40+40 refresh commits ≤ 6 times, not per component |

`Counter.rows_where(needle)` is how the page-scoping is pinned: it sums rows for statements
containing a SQL fragment, so the assertion names the query that must have shrunk.

Extended **`tests/test_connectors.py`**:

* `BlackDuckSharedOriginTests` (B6/B9) — a BOM of 6 components sharing one origin:
  `test_shared_origin_is_read_once_for_the_whole_bom` (origin GET ×1, collection GET ×1, total
  requests < component count, every component still gets its copyright),
  `test_a_second_inventory_read_asks_the_network_again` (the memo dies with the call),
  `test_each_new_notice_is_one_post_and_the_collection_is_read_once` (B9: exactly one POST with the
  one new notice, origin and collection read once each).
* `FossologyEvidenceTests` (B7) — `test_walk_stops_once_it_has_the_paths_it_asked_for` (the far
  branch is never requested), `test_a_walk_that_stopped_early_is_not_cached_as_the_whole_tree`,
  `test_walk_stops_at_the_depth_cap` rewritten to drive `_item_map` (a 60-deep chain stops at the
  cap), and `test_item_map_and_generic_text_are_cached` updated to the new caching rule.

Existing tests that must keep passing untouched: all of `test_matching.py`,
`test_matching_scale.py` (B1), `test_write_atomicity.py` (A10), `test_review.py`, `test_sync.py`,
`test_e2e.py`, `test_db_schema.py` (B2/B3), and the browser smoke test.

---

## 8. Red proof (each fix pinned by a failing-without-it test)

Ran the new/changed tests against a pristine HEAD with `SNAPSHOTS_PER_SIDE = 99` stubbed in so the
B5 tests could at least import: **18 failed, 41 passed**.

* B4 — `test_preview_statements_do_not_grow_with_the_queue`,
  `test_the_page_reads_only_its_own_components`,
  `test_listing_mappings_reads_connections_once`,
  `test_list_jobs_reads_scale_with_jobs_not_items`.
* B5 — `test_refresh_stops_copying_history_forever`,
  `test_matches_for_components_that_vanished_are_dropped`,
  `test_a_stale_match_is_kept_because_it_is_a_finding`.
* B6 — `test_shared_origin_is_read_once_for_the_whole_bom`
  (`AssertionError: 6 != 1 : the origin was resolved per component`).
* B7 — `test_walk_stops_once_it_has_the_paths_it_asked_for`,
  `test_a_walk_that_stopped_early_is_not_cached_as_the_whole_tree`.
* B8 — all seven commit tests.
* B9 — `test_each_new_notice_is_one_post_and_the_collection_is_read_once`.

The tests that pass on HEAD are the guards, not the fixes: preview content, queue row content,
job content, previous-snapshot survival and reviewer-history survival. They exist so the fixes
cannot be paid for with lost data.

---

## 9. Measured result (after)

| finding | before | after |
|---|---|---|
| B4 preview, N=50 / 200 / 800 | 705 / 2,805 / 11,205 stmts; 79.9 ms @ 800 | **21 / 21 / 28 stmts**; 15.8 ms @ 800 |
| B4 queue request, N=800 | 11 stmts / 5,604 rows | 11 stmts / 2,604 rows (page-scoped maps) |
| B4 queue bytes read, N=200 / 800 | 189,944 / 768,945 | **43,237 / 164,437** (4.4× / 4.7× less) |
| B4 10 mappings | 34 stmts / 44 rows | 3 stmts / 14 rows |
| B4 5 sync jobs | 111 stmts / 210 rows | 4 stmts / 130 rows |
| B5 after 9 refreshes (40 comps) | 18 snapshots / 720 components | **10 snapshots / 400 components** |
| B5 vanished components | rows linger forever | 5 dead rows deleted per refresh |
| B6 60 BOM comps, one shared origin | 121 HTTP requests | **3** |
| B7 tree walk, 20×20 noise | 33 requests / 23 tree walks | **12 / 2** |
| B7 3× noise dirs | 81 requests / 63 tree walks | **20 / 2** (flat in tree size) |
| B8 `refresh()` | 246 commits | **5** |
| B8 `decide()` / `add_note()` / job item | 3 / 2 / 2 | **1 / 1 / 1** |
| B9 `update_copyrights`, 2 notices | 5 requests | **4** (1 POST per new notice, by design) |
| suite | 183 passed, 85.15% | **208 passed, 86.36%** |
| B1 benchmark (must not regress) | 423 / 1,630 / 3,244 stmts; no-op 41 | 424 / 1,631 / 3,245 stmts; no-op 42 (+1 = the prune query) |

---

## 10. Commit split

1. `Stop the read fan-out in the queue, preview and listings (B4)`
   — `service.py`, `review.py`, `sync.py`, `api.py`, `tests/test_read_scale.py`.
2. `Bound snapshot and match growth (B5)`
   — `service.py`, `tests/test_read_scale.py`.
3. `Read each Black Duck origin and FOSSology directory once (B6, B7, B9)`
   — `connectors/blackduck.py`, `connectors/fossology.py`, `tests/test_connectors.py`.
4. `Commit each multi-statement write as one unit (B8)`
   — `db.py`, `review.py`, `service.py`, `sync.py`, `tests/test_read_scale.py`.

(Commits 2 and 4 both touch `service.py::_run_matching`; land 2 first so 4 only adds the
`DB.commits` counter and the remaining wraps.)

---

## 11. Verification

```bash
uv run pytest -q --ignore=adws                 # 208 passed, coverage gate green
uv run ruff check clreview tests               # exit 0
uv run python -m unittest discover -s tests -p 'test_*.py' -q   # 208, parity with pytest
uv run pytest tests/test_web_smoke.py -q --no-cov              # 1 passed
```

Then re-run the scratch benchmarks (`/tmp/bench_b4.py`, `/tmp/bench_b6b7.py`, `/tmp/bench_b8.py`,
`/tmp/bench_bytes.py`, `/tmp/bench_b1.py`) from the working tree and confirm the §9 table. Report
the new total coverage number.

## 12. Out of scope — do not touch

* B1/B2/B3 (done), and every A, C, D, E, F finding.
* No caching that outlives one call frame — no `functools.lru_cache` on service methods, no
  connector-level memo except the per-call cache in §4.
* No HTTP/2, no connection pooling, no thread-pool or `check_same_thread` changes.
* No new dependencies; no `--cov-fail-under` change.
* No rewrite of `_current_matches` into a single SQL statement with `EXISTS` — the key-only read is
  the cheap 90%; the SQL rewrite is a separate finding.
* No API contract change: every response keeps its current fields and ordering.
* Do not wrap any network call in `DB.transaction()`.
