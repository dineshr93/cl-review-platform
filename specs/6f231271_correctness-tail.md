# Plan — Attack step 6 (A5, A6, A7, A8, A9, A10, A11, A12): correctness tail

**Repo:** `/home/dinesh/repos/foss_data_reviewer` · **HEAD:** `455515d` (step 5: browser smoke hermetic)
**Source of truth:** `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md` → findings **A5–A12**
**Order:** A5 → A6 → A7 → A8 → A9 → A10 → A11 → A12, as the scout ordered them. Four commits (below).

Every fix and every test below was applied and run in a scratch copy of this repo before this plan was written.
Measured results are in §0 and §9. Two things worth knowing up front:

1. **A10 is the only risky one.** It changes the shared `DB` helper. The scratch run proves the existing
   concurrency test stays green and that a 1,600-component snapshot transaction blocks a concurrent reader for
   ~8 ms, so the lock hold is not a latency problem.
2. **A9 has a fourth leak the scout did not list** — Black Duck's `_get` lets `json.JSONDecodeError` escape for a
   2xx HTML body, exactly the FOSSology leak at `fossology.py:86`. Same defect class, same contract, a file the
   finding names. It is one `except` clause and it is in scope; it is called out in §A9 so the reviewer can cut it.

---

## 0. Baseline, measured on this machine (before any change)

```
uv run pytest -q --ignore=adws                        → 144 passed, coverage 83.69% (gate 75)
uv run ruff check clreview tests                      → exit 0
uv run python -m unittest discover -s tests -p 'test_*.py'   → 144 tests
uv run pytest tests/test_web_smoke.py -q --no-cov     → 1 passed
```

After the whole step (measured in scratch, §9):

```
uv run pytest -q --ignore=adws     → 179 passed, coverage 84.98%
uv run ruff check clreview tests   → exit 0
unittest discover                  → 179 tests (parity with pytest kept)
web smoke                          → 1 passed in 7.6 s
```

---

## 1. A5 — non-idempotent POSTs are retried in Black Duck

**File:** `clreview/connectors/blackduck.py` (`_send`, ~line 85)

`_send` passes `retries=self.retries` for every method, so a copyright POST that the server applied but answered
503 with is replayed and duplicates the statement. FOSSology already got this right (`fossology.py:_post`,
`retries=0`). PUT is idempotent by HTTP semantics and Black Duck's license/text writes are PUTs, so only the
non-idempotent methods lose the retry.

```python
    def _send(self, method: str, url: str, body: Any, media: str) -> dict[str, Any]:
        media = media or "application/json"
        # A POST the target applied but answered 5xx with would be replayed by a retry and
        # duplicate the write; PUT is idempotent, so only the non-idempotent methods lose it.
        retries = 0 if method.upper() in ("POST", "PATCH") else self.retries
        try:
            status, _, data = request(
                method, url, headers={**self._headers(media), "Content-Type": media}, body=body,
                timeout=self.timeout, retries=retries,
            )
```

Do **not** touch `update_licenses`' `put_json(..., retries=self.retries)` — that is a PUT and retrying it is correct.

**Test:** `tests/test_connector_contract.py::WriteRetryTests` (new file, §8). It drives the *real*
`http.request` over a fake transport that fails 503 once, and counts transport calls:

- `test_post_is_not_retried` → `calls == ["POST"]` and `ConnectorError` raised.
- `test_put_is_retried` → `calls == ["PUT", "PUT"]` and status 200.

---

## 2. A6 — silent pagination truncation

**Files:** `clreview/connectors/http.py` (`paginated`), `clreview/connectors/blackduck.py` (`_paginate`),
`clreview/connectors/fossology.py` (`_pages`)

Both walkers stop at a page cap and return what they have. A partial inventory in a compliance tool is
indistinguishable from components that do not exist, so the cap becomes an error.

**`http.py`** — add the exception and the cap constant next to the other module constants:

```python
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BODY_IN_ERROR = 300
MAX_PAGES = 200


class PaginationLimit(Exception):
    """A listing kept answering and the page cap was reached: the result is incomplete."""
```

`PaginationLimit` lives in `http.py` (not `base.py`) so `base.py` keeps importing `http.py` without a cycle.
`paginated` becomes:

```python
def paginated(get_page: Callable[[int], tuple[list[dict[str, Any]], int | None]], limit: int = 100,
              max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
    """Fetch pages until a page returns fewer than `limit` or total is reached.

    Reaching the cap with items still outstanding raises instead of returning what was
    collected: a silently short inventory in a compliance tool reads as components that do
    not exist.
    """
    out: list[dict[str, Any]] = []
    page = 0
    while True:
        items, total = get_page(page)
        out.extend(items)
        page += 1
        if not items or len(items) < limit or (total is not None and len(out) >= total):
            return out
        if page >= max_pages:
            raise PaginationLimit(
                f"listing stopped after {max_pages} pages of {limit} items ({len(out)} collected); "
                "refusing to report a partial inventory"
            )
```

**`blackduck.py`** — import `PaginationLimit` from `.http` and convert it at the connector boundary so the
documented contract (`ConnectorError`) still holds:

```python
        try:
            return paginated(page, limit)
        except PaginationLimit as exc:
            raise ConnectorError(str(exc)) from exc
```

**`fossology.py`** — `_pages` has its own loop; import `MAX_PAGES` from `.http` and use `for/else`:

```python
    def _pages(self, url: str, limit: int = 100) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            sep = "&" if "?" in url else "?"
            data, headers = self._get(f"{url}{sep}limit={limit}", page=page)
            items = data if isinstance(data, list) else (data or {}).get("items", [])
            out.extend(items)
            total = headers.get("X-Total-Pages") or headers.get("x-total-pages")
            if not items or len(items) < limit:
                break
            if total:
                try:
                    pages = int(total)
                except ValueError as exc:
                    raise ConnectorError(f"FOSSology sent a non-numeric X-Total-Pages: {total!r}") from exc
                if page >= pages:
                    break
        else:
            raise ConnectorError(
                f"listing stopped after {MAX_PAGES} pages of {limit} items ({len(out)} collected); "
                "refusing to report a partial inventory"
            )
        return out
```

(The `int(total)` guard is A9's third bullet; it belongs in this loop, so it is done here.)

**Tests:** `PaginationHonestyTests` — short page ends the walk, reported total ends the walk, endless listing
raises `PaginationLimit` (with `max_pages=4` so the test is instant), Black Duck `list_projects` over an endless
fake listing → `ConnectorError` containing "refusing", FOSSology `list_scans` likewise, and a non-numeric
`X-Total-Pages` → `ConnectorError` naming the header. The default cap is pinned with
`self.assertEqual(http_mod.MAX_PAGES, 200)`.

---

## 3. A7 — ScanCode license-expression parser invents entries

**File:** `clreview/connectors/scancode.py`

`str(expr).replace(" AND ", " ").replace(" OR ", " ").split()` turns `Apache-2.0 WITH LLVM-exception` into
`Apache-2.0`, `WITH`, `LLVM-exception` — two phantom licenses that flow into comparison and discrepancy counts.

Add a module-level parser (the scout's `domain.py` home is D2, which is out of scope; nothing else calls this
yet, so it lives next to its only user and moves when D2 lands). Add `import re`:

```python
EXPRESSION_OPERATORS = re.compile(r"\s+(?:AND|OR)\s+", re.IGNORECASE)


def split_license_expression(expression: str) -> list[str]:
    """One entry per operand of an SPDX expression.

    `AND` / `OR` separate operands and are matched as whole words surrounded by spaces, so the
    `or` inside `GPL-3.0-or-later` stays part of the identifier. `WITH` binds an exception to
    its license and is not a separator: `Apache-2.0 WITH LLVM-exception` is one license, not
    two. Parentheses only group, so they are stripped from the operands.
    """
    out: list[str] = []
    for part in EXPRESSION_OPERATORS.split(str(expression).strip()):
        name = part.strip().strip("()").strip()
        if name and name not in out:
            out.append(name)
    return out
```

**Why not the scout's literal `\b(?:AND|OR)\b`:** `-` is a non-word character, so `\bor\b` matches the `or` in
`GPL-3.0-or-later` and splits a real SPDX id in half. Requiring whitespace around the operator is both safe and
what SPDX canonical form actually looks like. This is the one place the plan deliberately deviates from the
scout's wording, and the test `test_or_inside_an_identifier_is_not_an_operator` is what proves it.

Replace both call sites:

- `_package`: `for part in split_license_expression(expr): licenses.append(LicenseEntry(name=part, spdx=part, source="scancode"))`
- `_grouped_files`: `for part in split_license_expression(expr): g["licenses"].setdefault(path, []).append(part)`

**Tests:** `LicenseExpressionTests` (unit, six cases: WITH stays whole, AND/OR split, parentheses, `GPL-3.0-or-later`,
lowercase operators, single license) and `ScanCodeExpressionTests` (end-to-end through `fetch_inventory` for both
the `packages` path and the `files` path, asserting the component carries exactly one license named
`Apache-2.0 WITH LLVM-exception`).

---

## 4. A8 — Black Duck license resolution scans only the first 100 hits

**File:** `clreview/connectors/blackduck.py` (`_resolve_license`, ~line 350)

```python
    def _resolve_license(self, name: str) -> str:
        # One page of 100 is not the answer: a common name has more hits than that and the exact
        # match can sit on a later page.
        data = self._paginate(join(self.base_url, f"/api/licenses?q=name:{quote(name)}"))
        items = data
```

`_paginate` appends `limit=100&offset=N*100` itself, so the hand-written `limit=100&offset=0` must go — leaving
both would send the parameter twice. `_paginate` already returns a list, so the `isinstance(data, dict)` branch
disappears with it.

**Test:** `LicenseResolutionTests::test_exact_match_beyond_the_first_hundred_hits` — a fake transport reads the
`offset` in the URL and puts the exact `zlib` hit only on page 2. Before the fix this raises
`ConnectorError("... not found in the knowledge base")`; after it, it returns the page-2 href.

---

## 5. A9 — raw exceptions escape `ConnectorError`

**Files:** `clreview/connectors/blackduck.py`, `clreview/connectors/fossology.py`

| site | leak | fix |
|---|---|---|
| `blackduck.test_connection` | `get_json` returns `None` for an empty 2xx body → `AttributeError` on `.get` | `data = self._get(...) or {}` |
| `blackduck._licenses` | Black Duck sends `"licenseType": null`; `.get(k, default)` returns `None` for a present-null key → `AttributeError` on `.lower()` | `(entry.get("licenseType") or "declared").lower()` |
| `fossology._get` | `json.loads` on a 2xx HTML/proxy page → bare `json.JSONDecodeError` | wrap → `ConnectorError("FOSSology returned a body that is not JSON: …")` |
| `fossology._pages` | `int(total)` on a garbage `X-Total-Pages` → bare `ValueError` | done in §2 with the A6 rewrite |
| **`blackduck._get` (not in the scout list)** | same JSON leak as FOSSology: `get_json` parses the body inside `http.py`, so a 2xx HTML page raises `json.JSONDecodeError` past the `except HttpError` | add `except json.JSONDecodeError` → `ConnectorError("Black Duck returned a body that is not JSON: …")` |

The Black Duck `_get` clause is the one addition beyond the finding's bullets. It is in a file A9 names, it is
the same contract leak, and it costs two lines. If the reviewer wants the step kept strictly to the listed
bullets, drop `test_blackduck_html_body_is_a_connector_error` and this clause together.

**Tests:** `ConnectorErrorContractTests` — Black Duck HTML body → `ConnectorError` containing "not JSON";
Black Duck 204 empty body → `{"ok": True, "tool": "blackduck", "total_projects": 0}`; `licenseType: null` →
`license_type == "declared"`; FOSSology HTML body → `ConnectorError` containing "not JSON".

---

## 6. A10 — no transaction around multi-statement writes

**File:** `clreview/db.py` then the three flows the finding names.

`DB.execute` commits every statement, so a write that dies halfway leaves a half-applied row set. Add a
transaction context manager that suspends the per-statement commit:

```python
    def __init__(self, path: str = ":memory:"):
        ...
        self.conn.row_factory = sqlite3.Row
        self._tx_depth = 0
```

```python
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            if self._tx_depth == 0:  # inside transaction() the commit waits for the block
                self.conn.commit()
        return cur

    def in_transaction(self) -> bool:
        return self._tx_depth > 0

    @contextmanager
    def transaction(self) -> Iterator["DB"]:
        """Commit a group of statements together, or not at all.

        ``execute`` commits per statement by default, so a write that dies halfway leaves a
        half-applied row set behind. This holds the write lock for the group, commits once at
        the end and rolls the whole group back on any error. Keep network calls out of the
        block: the lock is held for everything inside it.
        """
        with self.lock:
            outermost = self._tx_depth == 0
            self._tx_depth += 1
            try:
                if outermost:
                    self.conn.execute("BEGIN")
                yield self
                if outermost:
                    self.conn.commit()
            except BaseException:
                if outermost:
                    self.conn.rollback()
                raise
            finally:
                self._tx_depth -= 1
```

Imports: `from contextlib import contextmanager` and `from typing import Any, Iterator`.

Notes for the implementer:

- `self.lock` is an `RLock`, so `execute`/`query` inside the block re-enter it from the same thread. Other
  threads block until the block ends — that is the point, and it is why network calls stay out.
- The default `isolation_level` is left alone. An explicit `BEGIN` is legal because every `execute` commits, so
  no implicit transaction is open when the block starts. Verified on this machine's Python 3.13 with the
  existing concurrency test green.
- Nesting is supported (depth counter): a `transaction()` inside one does not commit early.

**Wrap exactly three flows** (the ones the finding names):

1. `service._store_snapshot` — split the row writes into `_store_snapshot_rows` and wrap the call:

```python
    def _store_snapshot(self, mapping_id: int, side: str, tool: str, inventory: list[ComponentData]) -> int:
        """Store one side's inventory as one unit: a half-stored snapshot would be matched."""
        version_hash = sha256(json.dumps(sorted(source_version(c) for c in inventory), sort_keys=True))
        with self.db.transaction():
            return self._store_snapshot_rows(mapping_id, side, tool, inventory, version_hash)
```

   `refresh()` keeps its `fetch_inventory()` call **outside** the transaction — that is the network call.

2. `review.decide_suggestion` accept path — `decide()` + `UPDATE suggestions` + audit in one block, returning
   outside it.

3. `sync.create_job` — job row + every item row + audit + the dry-run status update in one block;
   `self._execute(...)` (which talks to the PL) moves **outside** the block:

```python
        if not dry_run:
            self._execute(job_id, mapping, pl_conn, actor)
        return self.job(job_id)
```

`review.decide()` is **not** wrapped on its own: it is covered transitively when called from
`decide_suggestion`, and wrapping it separately is a refactor the finding does not name. Say so in the commit
message if asked.

**Tests:** `tests/test_write_atomicity.py` (§8). Each atomicity test patches `db.execute` with a wrapper that
raises `OSError` on a chosen statement prefix and compares row counts before/after:

- helper semantics: rollback leaves nothing, commit keeps the group, plain `execute` still commits, a nested
  block does not commit early, rows are visible inside the block.
- `_store_snapshot` failing on the first `INSERT INTO license_texts` → `snapshots`, `components`, `copyrights`,
  `licenses`, `license_texts` all unchanged.
- `decide_suggestion` accept failing on `UPDATE suggestions` → zero decisions, suggestion still `open`, no new
  audit row (compare against the audit count `add_suggestion` already wrote).
- `create_job` failing on the **second** `INSERT INTO sync_items` → zero `sync_jobs`, zero `sync_items`, no new
  audit row. A companion test asserts a successful dry run really produces ≥2 items, so the failure point is
  guaranteed to be mid-write.
- Positive controls for each path (the happy path still persists everything) so a broken rollback cannot pass
  by writing nothing at all.

---

## 7. A11 + A12 — 404 body text and bodyless requests

**File:** `clreview/api.py`

**A11.** `str(KeyError("x"))` renders the repr, quotes included, so 404 bodies read as
`"'match 999999 not found'"`. Unwrap at the handler, which fixes all seven `raise KeyError` sites at once and
leaves the router's use of `KeyError` alone:

```python
def _message(exc: BaseException) -> str:
    """`str(KeyError("x"))` renders the repr, quotes and all: a 404 body is not a traceback."""
    args = getattr(exc, "args", ())
    return str(args[0]) if args else str(exc)
```

```python
            except KeyError as exc:
                self._send(404, {"error": _message(exc)})
```

A `NotFoundError` subclass would be tidier but touches seven raise sites across four modules for a formatting
bug; that is the refactor this step's brief rules out.

**A12.** A chunked POST has no `Content-Length`, so `_body` reads `0` and hands the handler `{}`. Reject it:

```python
class LengthRequired(Exception):
    """A chunked body has no length to bound the read against, so it is not read at all."""
```

```python
    def _body(self) -> Any:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # Without a length there is no bound to read against, and an unread body would
            # desynchronise the connection. HTTP/1.0 closes after the response, so nothing to drain.
            raise LengthRequired("Content-Length required: chunked request bodies are not supported")
        length = int(self.headers.get("Content-Length") or 0)
        ...
```

and in `_dispatch`, before the `KeyError` clause:

```python
            except LengthRequired as exc:
                self._send(411, {"error": str(exc)})
```

`BaseHTTPRequestHandler.protocol_version` is still `HTTP/1.0`, so the connection closes after the response and
the unread body cannot desynchronise a kept-alive connection. A POST with genuinely no body (e.g.
`/api/mappings/<id>/refresh`, which the frontend calls that way) must keep working — that is what the fourth
test pins.

**Tests:** `ErrorBodyTests` appended to `tests/test_e2e.py` (§8). `Server.raw` gains an `extra=None` parameter
merged into its headers so the test can send `Transfer-Encoding: chunked` verbatim.

---

## 8. New and changed test files

| file | contents |
|---|---|
| `tests/test_connector_contract.py` (**new**) | A5 `WriteRetryTests`, A6 `PaginationHonestyTests`, A8 `LicenseResolutionTests`, A9 `ConnectorErrorContractTests`, A7 `LicenseExpressionTests` + `ScanCodeExpressionTests` — 21 tests |
| `tests/test_write_atomicity.py` (**new**) | A10 `TransactionHelperTests`, `SnapshotAtomicityTests`, `SuggestionAtomicityTests`, `JobAtomicityTests` — 10 tests |
| `tests/test_e2e.py` (append) | A11 + A12 `ErrorBodyTests` — 4 tests; `Server.raw` gains `extra=None` |

Shared harness in `tests/test_connector_contract.py` — reuse it, do not hand-roll per test:

```python
def _over(transport, fn):
    def call(*args, **kw):
        kw["transport"] = transport
        return fn(*args, **kw)

    return call


def real_http(module, transport):
    """Run a connector module's http helpers for real, over a fake transport."""
    stack = contextlib.ExitStack()
    for name in ("get_json", "get_text", "request", "put_json"):
        if hasattr(module, name):
            stack.enter_context(mock.patch.object(module, name, _over(transport, getattr(http_mod, name))))
    return stack
```

`hasattr` is required: `fossology` imports `request`/`get_text` but not `get_json`/`put_json`, and
`mock.patch.multiple` raises on a name the module does not have. This harness drives the real retry and status
logic, which is the whole point of the A5 and A6 tests — the existing `test_connectors.py` fake replaces
`request` wholesale and therefore cannot see retry counts.

`tests/test_write_atomicity.py` imports `base_components` from `test_review` (tests dir is on `sys.path` under
pytest and under `unittest discover`) and `make_service`/`make_review`/`tmpdir` from `helpers`.

---

## 9. Verification — run all of it, in this order

```bash
cd /home/dinesh/repos/foss_data_reviewer

# 1. full suite + coverage gate
uv run pytest -q --ignore=adws

# 2. lint
uv run ruff check clreview tests

# 3. the unittest/pytest collection parity gate from step 2 must stay closed
uv run python -m unittest discover -s tests -p 'test_*.py' -q

# 4. the browser smoke test still passes (it exercises POSTs through the changed _body)
uv run pytest tests/test_web_smoke.py -q --no-cov

# 5. the scale work from step 4 must not have regressed under the new transaction
uv run python /tmp/bench_b1.py   # or the equivalent scratch benchmark

# 6. red/green proof per finding (see table below)
git stash && uv run pytest tests/test_connector_contract.py tests/test_write_atomicity.py -q --no-cov; git stash pop
```

Measured in scratch:

| check | before | after |
|---|---|---|
| `pytest -q --ignore=adws` | 144 passed, 83.69% | **179 passed, 84.98%** |
| `ruff check clreview tests` | exit 0 | exit 0 |
| `unittest discover` | 144 | 179 (parity kept) |
| web smoke | 1 passed | 1 passed, 7.6 s |
| B1 benchmark N=1600 first matching | 0.10 s / 3,244 statements | 0.12 s / 3,244 statements |
| B1 benchmark N=1600 no-op refresh | 0.05 s / 41 statements | 0.05 s / 41 statements |
| `_store_snapshot` of 1,600 components in one transaction | n/a | 0.01 s; concurrent reader blocked ≤ 8.1 ms |

Red proof per finding (new tests run against unmodified `HEAD`):

| file | result without the fixes |
|---|---|
| `tests/test_connector_contract.py` | collection error: `cannot import name 'PaginationLimit'` (A6/A7 names do not exist yet) |
| `tests/test_write_atomicity.py` | 7 failed, 3 passed (the 3 are the positive controls) |
| `tests/test_e2e.py -k ErrorBody` | 3 failed, 1 passed (the passing one is the bodyless-POST guard) |

Report the new total coverage number when done.

---

## 10. Commits — four, in this order

1. `Stop retrying non-idempotent writes and stop truncating listings (A5, A6, A8)`
   — `blackduck.py`, `http.py`, `fossology.py` (pagination half), `tests/test_connector_contract.py`
2. `Parse SPDX expressions without inventing licenses (A7)`
   — `scancode.py`, `tests/test_connector_contract.py`
3. `Keep connector failures inside the ConnectorError contract (A9)`
   — `blackduck.py`, `fossology.py`, `tests/test_connector_contract.py`
4. `Commit multi-statement writes as a unit and clean up two HTTP body edges (A10, A11, A12)`
   — `db.py`, `service.py`, `review.py`, `sync.py`, `api.py`, `tests/test_write_atomicity.py`, `tests/test_e2e.py`

If the split fights you (the three connector files are touched by 1, 2 and 3), one commit for A5–A9 and one for
A10–A12 is acceptable; two commits, both honest.

---

## 11. Out of scope — do not touch

- Every B, C, D, E, F finding; A13 (already fixed in `2d80668`); A14+ if any.
- `domain.py` — the shared-expression-helper move is D2.
- `review.decide()` on its own, `sync._execute`, the recompute path, and any other multi-statement write the
  finding does not name.
- The API contract beyond what A11/A12 force: no new routes, no changed status codes other than the new 411, no
  `NotFoundError` refactor across the domain layer.
- Frontend code: the app already renders `payload.error` verbatim, which is why A11 is a body-text fix only.
- New dependencies, new config keys, new CLI flags.
- `functools.lru_cache`, connection pooling, or any caching as a side quest.

## 12. Risks and how the plan handles them

| risk | handling |
|---|---|
| `DB.transaction()` deadlocks or breaks the threaded server | `RLock` is re-entrant; the existing `tests/test_db_concurrency.py` (8 threads × 40 statements) stays green in scratch; no network call is inside any block |
| A long transaction blocks readers | measured: 1,600-component store = 0.01 s, concurrent reader blocked ≤ 8.1 ms |
| Explicit `BEGIN` collides with sqlite3's implicit transactions | every `execute` commits, so no implicit transaction is open at `BEGIN`; verified on Python 3.13 with the full suite |
| A6 turns a working (if wrong) listing into a hard failure | that is the finding's intent — "turns a silent wrong answer into a loud failure"; the cap is unchanged at 200 pages × 100 items |
| A12 breaks a legitimate POST | the bodyless-POST test pins `/api/mappings/<id>/refresh`; the browser smoke test exercises every other POST the UI makes |
| A7's whitespace rule misses an expression with no spaces around `AND` | SPDX canonical form always has them; the lowercase-operator test covers the non-canonical case that does appear in the wild |
