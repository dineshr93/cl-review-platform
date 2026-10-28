# Step 9 — Test tail: C5, C7, C8

Session `6f231271`. Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`.
HEAD at planning time: `d743075` (steps 1–8 landed; C1–C4 and C6 are done — do not redo them).
A, B, D, E, F findings are out of scope.

**Baseline measured at HEAD on this machine:**

| measure | value |
|---|---|
| `uv run pytest -q --ignore=adws` | **223 passed**, 57 warnings, 60 subtests passed, 30.4 s |
| coverage | **88%** (TOTAL 2921 statements, 347 missing), gate `--cov-fail-under=75` |
| `uv run ruff check clreview tests` | exit 0 |
| `uv run python -m unittest discover -s tests -p 'test_*.py' -q` | `Ran 223 tests` — parity held |
| `raise` sites in `clreview/` | **104** |
| `assertRaises` / `pytest.raises` in `tests/` | 46 |
| `/tmp/clreview-test-*` dirs on this box | **5,002** (scout saw 569; steps 3–8 added the rest) |
| `/tmp/tmp*` dirs (bare `mkdtemp`, no prefix) | 89 |
| dirs leaked by **one** suite run | **70** (measured: 4,792 → 4,862) |
| unclosed-sqlite `ResourceWarning`s | present in the warnings summary, attributed to `tests/test_read_scale.py` (4) and `tests/test_matching.py` (1) |

The raise audit below was produced by a script over `clreview/**/*.py` that extracts each `raise`,
its exception type and every literal segment of its message (f-string placeholders split out), then
greps those segments across `tests/*.py`. Reproduce it with the snippet in §1.4. Tiers:

| tier | count | meaning |
|---|---|---|
| message asserted in a test | **45** | a test asserts a ≥8-char fragment of the message |
| only a short fragment appears | **2** | `http.get_json` / `http.get_text` — `"GET {url} failed"` |
| no message of its own (re-wrap) | **8** | `raise ConnectorError(str(exc), status=exc.status)` — the message is the inner one |
| **no fragment of the message anywhere in `tests/`** | **49** | the C5 target |

---

## 0. Ground rules

* Runner is `uv`. Gates: `uv run pytest -q --ignore=adws` (coverage gate), `uv run ruff check clreview
  tests`, and `uv run pytest tests/test_web_smoke.py -q --ignore=adws`.
* Single-file runs need `--no-cov` (`--cov` lives in `pytest` `addopts`).
* New tests are `unittest.TestCase` classes — both runners must keep collecting the same count.
* **No new dependencies.** No property-based or fuzzing frameworks. No `pytest`-only fixtures beyond
  the one session-scoped cleanup fixture in §1.
* **Every new test must fail without the code it pins.** Prove it the way steps 6–8 did: copy the
  tree to a scratch dir, delete or neuter the guard, run the new test file, record the failure count.
  A test that passes with the guard removed pins nothing — delete it or fix it.
* `clreview/` changes only where a test exposes a **real defect**. §3 names the two candidates and
  says what to do with each. Do not change behaviour silently, and do not change a route, status
  code or response shape.
* No test that restates an implementation detail (e.g. "assert the private helper returns a dict with
  key x"). Each test names a user-visible contract: a status code, a message an operator reads, a
  row that must or must not exist.

### Ordering deviation, stated on purpose

The prompt lists C5 → C7 → C8. **Do C8 first.** Every C5/C7 test creates a temp directory through
`helpers.tmpdir()`; writing ~40 new tests before fixing the leak adds ~40 more permanent dirs and
makes the C8 gate ("no temp dirs survive") harder to show afterwards. C8 is three lines. The rest of
the order is as asked.

---

## 1. C8 — fixtures leak temp dirs (do first)

### 1.1 Evidence

`tests/helpers.py:128-129` is the whole leak:

```python
def tmpdir() -> str:
    return tempfile.mkdtemp(prefix="clreview-test-")
```

No cleanup, and **19 call sites** inherit it:

```
tests/test_review.py:36, 93, 120, 145, 240, 281      tests/test_sync.py:53, 93, 104, 153
tests/test_write_atomicity.py:89, 120, 150           tests/test_read_scale.py:88
tests/test_suggestions.py:24                         tests/test_connectors.py:486, 507
tests/test_connector_contract.py:223
```

Two sites bypass the helper:

* `tests/test_db_schema.py:47-48` — `tempfile.mkdtemp(prefix="clreview-test-")` **with**
  `self.addCleanup(shutil.rmtree, ...)` (the only correct one).
* `tests/test_e2e.py:328` — `root = tempfile.mkdtemp()` in
  `StaticPathTests.test_a_sibling_of_the_web_root_is_not_served`. No cleanup **and no prefix**, so it
  is invisible to the scout's `ls /tmp/clreview-test-*` check. That is where the 89 `/tmp/tmp*` dirs
  come from.

Already clean, leave alone: `tests/test_connectors.py:556,568` use
`with tempfile.TemporaryDirectory()`.

Second leak of the same family: sqlite connections. `tests/test_read_scale.py:90` opens
`DB(":memory:")` in `setUp` and never closes it (`test_matching.py:107-108` shows the correct
pattern). `clreview/db.py:154` already provides `DB.close()`.

### 1.2 Fix — clean up at the source, not at 19 call sites

`tempfile.TemporaryDirectory` already deletes itself when the object is collected. Keep the objects
alive in a module list and drop them at session end:

```python
# tests/helpers.py  (replaces lines 128-129)
_OWNED: list[tempfile.TemporaryDirectory] = []


def tmpdir() -> str:
    """A temp dir that is removed when the session ends. Callers own none of the bookkeeping."""
    d = tempfile.TemporaryDirectory(prefix="clreview-test-")
    _OWNED.append(d)
    return d.name


def release_tempdirs() -> None:
    """Delete everything handed out by tmpdir(). Called once, at session end."""
    while _OWNED:
        _OWNED.pop().cleanup()
```

```python
# tests/conftest.py  (new file — tests/ has no conftest.py today)
"""Session-wide cleanup so a suite run leaves nothing in /tmp."""
import pytest

import helpers


@pytest.fixture(scope="session", autouse=True)
def _no_temp_dirs_left_behind():
    yield
    helpers.release_tempdirs()
```

`tests/` is already importable as a plain directory (`from helpers import tmpdir` works today), so
`import helpers` in `conftest.py` needs no `__init__.py` and no path fiddling. If it does fail, fall
back to `from tests import helpers` and say so in the commit body — do not add a `sys.path` hack.

Then two one-line edits:

* `tests/test_e2e.py:328` — `root = tempfile.mkdtemp()` → `root = tmpdir()` (import it; this also
  gives the dir the `clreview-test-` prefix so the gate can see it).
* `tests/test_read_scale.py` — add `self.addCleanup(self.db.close)` next to `self.db = DB(...)` at
  line 90, and do the same for any other `DB(` in a `setUp` without a matching close (check
  `tests/test_matching.py`'s scale test, which produced one of the warnings).

Do **not** rewrite the 19 call sites. Do not add a `tearDown` to 7 files. One helper, one conftest.

### 1.3 Gate

```bash
rm -rf /tmp/clreview-test-*                       # clear the 5,002 historical dirs once
before=$(ls -d /tmp/clreview-test-* 2>/dev/null | wc -l)   # expect 0
uv run pytest -q --ignore=adws
after=$(ls -d /tmp/clreview-test-* 2>/dev/null | wc -l)    # must be 0
uv run pytest -q --ignore=adws                            # run it twice, back to back
ls -d /tmp/clreview-test-* 2>/dev/null | wc -l            # must still be 0
```

Do **not** `rm -rf /tmp/tmp*` — those dirs may belong to other programs. Prove the `test_e2e.py:328`
fix by counting `/tmp/tmp*` before and after a run: the count must not grow.

Report: dirs leaked per run **before (70) and after (0)**, and the warning count before/after
(baseline 57, including the unclosed-sqlite ones).

### 1.4 Reproducing the raise audit

```bash
uv run python - <<'PY'
import re, glob
TESTS = "\n".join(open(f, encoding='utf-8').read() for f in glob.glob('tests/*.py'))
tot = un = 0
for f in sorted(glob.glob('clreview/**/*.py', recursive=True)):
    src = open(f, encoding='utf-8').read().splitlines()
    for i, line in enumerate(src, 1):
        if 'raise ' not in line: continue
        stmt = line.strip(); j = i
        while stmt.count('(') > stmt.count(')') and j < len(src):
            stmt += ' ' + src[j].strip(); j += 1
        if not re.search(r'raise \w+', stmt): continue
        tot += 1
        segs = {s.strip(' .,:;') for lit in re.findall(r'"([^"]{6,})"|\'([^\']{6,})\'', stmt)
                for s in re.split(r"\{[^{}]*\}", lit) if len(s.strip(' .,:;')) >= 6}
        if segs and not any(s in TESTS for s in segs):
            un += 1; print(f"{f}:{i} {stmt[:90]}")
print(f"{un} of {tot} raise messages appear nowhere in tests/")
PY
```

Use it after the work: the number must fall from **49** to near zero, and the remaining entries must
be justified in the commit body (the re-wraps and anything you argue is not worth pinning).

---

## 2. C5 — the raises that matter

New file **`tests/test_error_contract.py`**, one class per module, each class a loop of
`(callable, exception type, message)` triples — the pattern already at
`tests/test_connectors.py:318-323`:

```python
for call in (lambda: conn.update_licenses("k", []), ...):
    with self.assertRaises(UnsupportedOperation):
        call()
```

Extend it to `assertRaisesRegex` (or `assertEqual` on `str(exc)`) so the **message** is pinned, not
just the type. Use `re.escape` on messages that contain regex characters (`(`, `)`, `$`).

Every entry below is `UNTESTED` per the audit (no fragment of its message appears in `tests/`).
Triggers are the shortest realistic ones.

### 2.1 `Service` — 7 sites (`ServiceGuardTests`) — 6 new, 1 tighten

| site | exception | message | trigger |
|---|---|---|---|
| `service.py:52` | `ValueError` | `role must be 'pl' or 'sl'` | `svc.add_connection("middle", "mock-pl", …)` |
| `service.py:56` | `ValueError` | `connection config must not carry credentials: {names}; name the environment variable in auth_env instead` | **tighten, do not duplicate**: `CredentialTests.test_literal_credential_never_reaches_persisted_state` (`tests/test_connectors.py:78`) already fires the A3 guard and asserts a substring. Extend that assertion to the full message including the named keys |
| `service.py:126` | `ValueError` | `project discovery requires a project-level connection` | `list_projects(<sl connection id>)` |
| `service.py:136` | `ValueError` | `scan discovery requires a source-level connection` | `list_scans(<pl connection id>)` |
| `service.py:144` | `ValueError` | `folder discovery requires a source-level connection` | `list_folders(<pl connection id>)` |
| `service.py:147` | `ValueError` | `f"{conn['tool']} has no folder concept to deploy into"` | an SL connection whose tool has no `list_folders` — **`mock-sl` does have one** (`mock.py:226`), so use a `scancode` SL connection (`scancode.py` defines `list_scans` and no `list_folders`) |
| `service.py:160` | `ValueError` | `mapping requires one PL connection and one SL connection` | `create_mapping(..., pl=sl_id, sl=sl_id)` |

`service.py:83` / `:92` (`connection {id} not found`) and `:180` (`mapping {id} not found`) are
already pinned through the HTTP layer (`tests/test_e2e.py:262` asserts `"not found"`); leave them.

### 2.2 `ReviewService` — 11 sites (`::ReviewGuardTests`) — 6 new, 5 tighten

| site | exception | message | trigger |
|---|---|---|---|
| `review.py:426` | `ValueError` | `unknown field {field!r}` | `decide(mid, field="colour", action="approve_sl", key="x")` |
| `review.py:428` | `ValueError` | `unknown action {action!r}` | `decide(..., field="copyright", action="obliterate")` |
| `review.py:467` | `ValueError` | `unknown field {field!r}` | `add_suggestion(..., field="colour")` |
| `review.py:492` | `ValueError` | `suggestion decision must be accept or reject` | `decide_suggestion(sid, "maybe", …)` |
| `review.py:522` | `ValueError` | `invalid review state` | `set_review_state(key, "half_done", …)` |
| `review.py:490` | `KeyError` | `suggestion {suggestion_id} not found` | `decide_suggestion(999999, "accept")`; over HTTP `POST /api/suggestions/999999/decision {"action":"accept"}` → 404 with a clean body (the A11 pattern). **Nothing reaches this today** |
| `review.py:529` | `ValueError` | `{n} discrepant field(s) still undecided` | **tighten**: *triggered* type-only by `ReviewTests.test_approve_for_sync_requires_decisions` (`tests/test_review.py:66-67`) and by `tests/test_suggestions.py:33`. Verified render: `3 discrepant field(s) still undecided` |
| `review.py:531` | `ValueError` | `no approved SL-derived correction to synchronize` | **tighten**: *triggered* type-only by `ReviewTests.test_unmatched_component_cannot_be_approved` (`tests/test_review.py:119`, bare `assertRaises` at `:125`) |
| `review.py:542` | `ValueError` | `both sides must be assigned before confirmation` | **tighten**: substring-pinned by `ConfirmReassignTests.test_confirm_requires_both_sides` (`tests/test_review.py:166`, `assertIn("both sides", …)`) and `tests/test_e2e.py:144-146` |
| `review.py:558` | `ValueError` | `PL component {new_pl!r} not in current snapshot` | **tighten**: *triggered* type-only at `tests/test_review.py:188-189` (`reassign(matched, pl_key="nope")`) |
| `review.py:560` | `ValueError` | `SL component {new_sl!r} not in current snapshot` | **tighten**: substring-pinned by `ConfirmReassignTests.test_reassign_rejects_a_key_that_is_not_in_the_snapshot` (`tests/test_review.py:184`) and `tests/test_e2e.py:152-156` |

The eight `decide()` value-resolution messages (`review.py:380-419`, `:442`) are already pinned
verbatim, service-level and over HTTP, by `DecisionValidationTests` (`tests/test_review.py:251+`) —
that is step 8's work; do not re-pin them.

### 2.3 `SyncService` — 10 sites (`::SyncGuardTests`)

| site | exception | message | trigger |
|---|---|---|---|
| `sync.py:157` | `ValueError` | `live synchronization requires confirm=true` | `create_job(..., live=True, confirm=False)` — today only `assertIn("confirm", …)` at `test_e2e.py:131`, which any message containing "confirm" would pass |
| `sync.py:160` | `ValueError` | `PL connection has not passed a connectivity test; synchronization stays disabled` | create a mapping whose PL was never tested |
| `sync.py:175` | `ValueError` | `no component is approved for synchronization` | `create_job` on a fresh mapping |
| `sync.py:177` | `ValueError` | `{n} items exceeds the {max}-item safety limit; narrow the selection` | `SyncService(svc, max_items=1)` then a 2-item job |
| `sync.py:232` | `UnsupportedOperation` | `caps.notes or "copyright writes unsupported"` | see the note below |
| `sync.py:240` | `UnsupportedOperation` | `caps.notes or "license name writes unsupported"` | see the note below |
| `sync.py:249` | `UnsupportedOperation` | `caps.notes or "license text writes unsupported"` | see the note below |
| `sync.py:384` | `ValueError` | `dry-run jobs have nothing to retry; create a live job` | retry a dry-run job |
| `sync.py:386` | `ValueError` | `retry requires confirm=true` | retry a live job without confirm |
| `sync.py:389` | `ValueError` | `no failed items to retry` | retry a job whose items all succeeded |

`sync.py:370` (`sync job {id} not found`) is pinned verbatim over HTTP by
`ErrorBodyTests.test_another_404_site_is_equally_clean` (`tests/test_e2e.py:364`); leave it.

**Three things about this table that change how the tests get written:**

1. **`157/160/175/177` are already *fired*, type-only.** `SyncTests.test_safety_rails`
   (`tests/test_sync.py:137-148`) raises all four under bare `assertRaises(ValueError)` — four
   identical assertions that cannot tell one site from another. **Tighten that one test in place**
   (capture `ctx.exception` and assert the message) instead of adding four parallel tests. `157` is
   also substring-pinned at `tests/test_e2e.py:128`.
2. **`384/386/389` are never reached at all** — grepping `"nothing to retry"`, `"retry requires"` and
   `"no failed items"` across `tests/` returns nothing. Triggers: dry-run job then
   `retry(id, confirm=True)`; a live job with a failed item then `retry(id)` with no confirm; a clean
   job whose items all succeeded then `retry(id, confirm=True)`.
3. **`232/240/249` do not surface as exceptions.** `_execute` catches them at `sync.py:259` and stores
   the item as `unsupported_target`, so **assert on the `sync_items` row, not with
   `assertRaises`**. They are also unreachable through a normal job: `preview_match` already stamps
   `unsupported_target` when the capability is off (`sync.py:96,105,115`) and `_execute` skips those
   statuses (`sync.py:208`) — but `retry` re-queues items with status `"ready"` (`sync.py:396`),
   which bypasses the skip. So: build a live job whose item fails (`components(fail_writes=True)`,
   the pattern at `tests/test_sync.py:104-110`), patch the connector's `capabilities` to turn the
   write off, then `retry(job["id"], confirm=True)`. Note `caps.notes` **wins** over the fallback
   string, so with `helpers.ReadOnlyPL` the stored message is `"test target: no write endpoints"`,
   never `"copyright writes unsupported"` — pick a connector whose `notes` is empty if you want to
   pin the fallback.

### 2.4 `deploy` — 6 sites (`::DeployGuardTests`)

`tests/test_deploy.py` fakes the resolver, not the subprocess. Four of these six are new; two are
substring-pinned and should be **tightened where they are**, not duplicated:

| site | exception | message | trigger |
|---|---|---|---|
| `deploy.py:98` | `ConnectorError` | `{command!r} not found on PATH; install it with 'uv pip install id2url' or set config.id2url_command` | **tighten, do not duplicate**: `Id2UrlContractTests.test_missing_binary_is_a_clear_error` (`tests/test_deploy.py:76-80`) already asserts `assertIn("id2url", …)`, which any message containing that word passes. Add the `{command!r}` repr and the install hint to that assertion |
| `deploy.py:108` | `ConnectorError` | `id2url batch timed out after {timeout}s` | `subprocess.run` raises `subprocess.TimeoutExpired` |
| `deploy.py:113` | `ConnectorError` | `id2url batch produced no output (exit {code}): {stderr}` | fake `run` returning `returncode=1, stdout=""` |
| `deploy.py:118` | `ConnectorError` | `id2url batch returned unparsable output: {text[:200]}` | fake `run` returning `stdout="not json"` |
| `deploy.py:148` | `ConnectorError` | `the {role} connection cannot deploy (missing {needed}())` | the existing doubles satisfy `hasattr`, so use a real connector that does not: `MockSLConnector` has `ensure_folder` (`mock.py:230`) but no `source_coordinates`, so passing it as the PL reaches this guard → HTTP 502 |
| `deploy.py:214` | `ConnectorError` | `a FOSSology parent folder is required (request parent_folder or connection config deploy_parent_folder)` | **tighten, do not duplicate**: already substring-pinned by a `DeployTests.test_parent_*` case — read it and extend the assertion to the full message |

Mock targets, verified: patch `clreview.deploy.shutil.which` and `clreview.deploy.subprocess.run`
(deploy imports the modules, so patch through `deploy`'s names, not the globals). For the timeout
case use `side_effect=subprocess.TimeoutExpired(cmd=["id2url", "batch"], timeout=900)`; for the other
two, `return_value=SimpleNamespace(returncode=1, stdout="", stderr="fatal: boom")` and
`stdout="not json"` respectively.

### 2.5 Connectors — 13 messages (extend `tests/test_connector_contract.py`)

Use the harness that file already defines — do not build a second one:

| helper | what it gives you |
|---|---|
| `real_http(module, transport)` (`test_connector_contract.py:37`) | an `ExitStack` that patches whichever of `get_json` / `get_text` / `request` / `put_json` the connector module actually imported, each wrapped by `_over` (L30) to inject `transport=` — so the connector's real HTTP path runs against a fake transport `def transport(method, url, headers, body, timeout) -> (status, headers, bytes)` |
| `blackduck(**cfg)` / `fossology(**cfg)` (L42, L44) | connectors with `auth_env` token `"t"` |
| `full_page(n)` (L49) | a transport that answers every page with `n` items and `x-total-pages: 999999` |
| `BlackDuckWriteTests.patch_api` / `with_api` (`test_connectors.py:110`, `:148`) | route-dict fakes for the four Black Duck HTTP names, recording `(method, url, headers, payload)` |

**Why the FOSSology internals are bare:** `FakeFossology` (`test_connectors.py:333`) overrides `_get`
and `_get_text`, so every raise inside those two methods is bypassed by the existing tests. New
status-path tests must go through `real_http(foss_mod, transport)`, not `FakeFossology`.

Three connector raises the audit flagged are **already pinned** — do not duplicate them:
`blackduck.py:112` and `fossology.py:94` (both asserted by `assertIn("not JSON", …)` at
`test_connector_contract.py:163` and `:188`) and `fossology.py:133` (non-numeric header, asserted at
`test_connector_contract.py:125`). Add:

| site | exception | message | trigger |
|---|---|---|---|
| `base.py:140` | `ConnectorError` | `unknown tool {tool!r}; known: [...]` | `create_connector("nope", "pl", …)` |
| `base.py:142` | `ConnectorError` | `tool {tool!r} cannot act as {role!r}; supported roles: {...}` | `create_connector("mock-sl", "pl", …)` |
| `blackduck.py:77` | `ConnectorError` | `Black Duck token missing (set the environment variable named in auth_env.token)` | **type-only today** — `CredentialTests.test_missing_token_fails_without_leaking` (`test_connectors.py:72`) asserts the type and that the token is not leaked, never the message. Tighten that test instead of adding one |
| `blackduck.py:277` | `ConnectorError` | `BOM component has no origins; Black Duck stores copyrights per origin` | `update_copyrights` on a component with no origins |
| `blackduck.py:294` | `ConnectorError` | `BOM component declares no license; there is no license text resource to update` | `update_license_text` on a license-less component |
| `blackduck.py:305` | `ConnectorError` | `BOM component declares {n} licenses ({names}); cannot decide which license text {file} belongs to` | **partially pinned** — `test_connectors.py:199` asserts only `assertIn("cannot decide", …)`. Pin the count/names/file-name clause, or drop the row |
| `fossology.py:74` | `ConnectorError` | `FOSSology credentials missing (auth_env.token or auth_env.username/password)` | `FossologyConnector("https://f", {}, {}).test_connection()` — `UnsupportedMessageTests` builds this connector but never touches `_headers` |
| `fossology.py:207` | `ConnectorError` | `FOSSology did not return folder {name!r} under parent {parent_id}` | `ensure_folder` where the create response omits the folder |
| `mock.py:103` | `ConnectorError` | `bad component key {key!r}` | `read_component("nonsense")` on the mock PL |
| `mock.py:158` | `ConnectorError` | `simulated PL API failure (500)` | the mock's own guard key — this is the fixture the sync tests should be using instead of their private `Boom` monkeypatch (`tests/test_write_atomicity.py:18-30`); pin it, do not rewire the existing test |
| `scancode.py:66` | `ConnectorError` | `ScanCode connector requires config.json_path (or its env variable)` | `ScanCodeConnector(config={})` |
| `scancode.py:72` | `ConnectorError` | `ScanCode JSON not found: {path}` | a path that does not exist |
| `fossid.py:37,40,43,46` | `ConnectorError` | `FOSSID adapter not implemented for {op} (no verified API mapping)` | the four unimplemented methods — `read_component` is already pinned at `tests/test_connectors.py:621-629`; pin the other four the same way, including `unsupported=True` |

### 2.6 HTTP status mapping — the re-wraps and the quirk (same file)

The 8 re-wrap sites (`raise ConnectorError(str(exc), status=exc.status)`) carry no message of their
own, so pin what they *do*: the status and the `unsupported` flag survive the wrap.

* `fossology.py:86` — the **412 → `unsupported=True`** quirk:
  ```python
  if status >= 400:
      raise ConnectorError(f"GET {url} failed with {status}", status=status,
                           unsupported=status == 412)
  ```
  Assert 412 sets `unsupported` and 500 does not. Today nothing in `tests/` mentions 412.
* `blackduck.py:97` — `f"{method} {url} failed with {status}"`: reached today only by
  `WriteRetryTests.test_post_is_not_retried` (`test_connector_contract.py:61`), which asserts the
  exception **type** and nothing else. Assert the status is carried for **401, 403, 404, 409, 429,
  500** as one loop under `real_http(bd_mod, transport)`.
* `http.py:45` — `connection failed: {reason}` (a `URLError`), and `http.py:80` —
  `request failed` when every retry was exhausted. Both need a transport stub, not a fake server.
* `http.py:86,95` — `GET {url} failed` from `get_json` / `get_text` on a non-2xx.

### 2.7 What C5 deliberately does **not** chase

* `api.py:248` (`{name} is required`) and `api.py:296` (`Content-Length required: chunked request
  bodies are not supported`) — the audit calls these unpinned, but `tests/test_e2e.py:228` asserts
  `"required"` in the body and `ErrorBodyTests` covers the chunked case. **Verify before writing**;
  if the assertion is only `"required"`, tighten it to the full message rather than adding a test.
* The 8 re-wraps beyond the status passthrough in §2.6.
* Anything whose only trigger is a `MemoryError`/OS fault.

---

## 3. C7 — missing negative and edge scenarios

New file **`tests/test_edge_scenarios.py`**. Each row is verified-untested at HEAD.

### 3.1 A connector failure during `refresh()` writes an `error` snapshot

`clreview/service.py:238-247`:

```python
try:
    inventory = conn.fetch_inventory(*args)
    sid = self._store_snapshot(mapping_id, side, conn.tool, inventory)
    results[side] = {"status": "ok", "count": len(inventory), ...}
except ConnectorError as exc:
    sid = self.db.execute(
        "INSERT INTO snapshots(mapping_id,side,tool,retrieved_at,status,error) VALUES(?,?,?,?,?,?)",
        (mapping_id, side, conn.tool, now(), "error", str(exc)),
    ).lastrowid
    results[side] = {"status": "error", "error": str(exc), "snapshot_id": sid}
```

No test greps for the `"error"` snapshot status. Assert: the failing side's snapshot row has
`status='error'` and the `error` text; the **other** side still stored `ok`; the old good snapshot is
still there (the failed refresh must not erase the last known state); matching still ran.

### 3.2 `overview` side statuses `never` and `error`

`clreview/service.py:441-442`:

```python
"status": last["status"] if last else "never",
"error": last["error"] if last else None,
```

Assert `overview` on a mapping that was never refreshed reports `"never"` for both sides, and after
the §3.1 failure reports `"error"` with the message. Today no test contains the string `never`.

### 3.3 `_run_matching` reports `partial` when one side has no successful snapshot

`clreview/service.py:378`:

```python
return {"status": "partial", "pl": len(pl), "sl": len(sl), "reason": "one side has no successful snapshot"}
```

Trigger: refresh once cleanly, then break one connector and refresh again so that side's newest
snapshot is the `error` one. Assert `matching["status"] == "partial"` and the reason. (The
`"partial"` hits in `tests/test_connector_contract.py:104` and `tests/test_connectors.py:466` are
different strings — pagination and tree-walk — they do not cover this.)

### 3.4 Empty inventories

Both sides return zero components. Assert: refresh reports `count: 0`, matching returns a zero-match
result rather than raising, the queue is empty, and `sync.create_job` still refuses with
`no component is approved for synchronization` rather than a different error.

### 3.5 Empty audit list

`GET /api/audit` (and the service method behind it) on a fresh DB with no audit rows → `200` and an
empty list, not a 500 and not a missing key.

### 3.6 `decisions` upsert overwrite

Decide the same `match_id + field` twice with different values and assert exactly one decision row
survives, holding the second value, with the second actor and timestamp. Quote the SQL in
`clreview/db.py` / `review.py` in the test docstring so the reader knows which clause is pinned.
(If the schema has no `ON CONFLICT` clause and instead inserts a second row, **that is the defect** —
see §3.9.)

### 3.7 `blackduck.fetch_inventory` duplicate-key dedup

Two BOM components that resolve to the same external key must collapse to one component, and the
surviving one must keep both origins' copyrights rather than the first one's. Quote the dedup block
in `clreview/connectors/blackduck.py` (around `:150-155` per the scout; re-read it — the file moved in
steps 6–8) in the test docstring.

### 3.8 A two-thread race at the HTTP level

`tests/test_db_concurrency.py` hammers `DB` directly from 8 threads; nothing races two **HTTP**
requests. Add one test that starts the real `Server` and fires two concurrent
`POST /api/components/{id}/decisions` for the same match and field from two threads, then asserts:
both return 2xx, exactly one decision row exists, and it holds one of the two values (not a mix).
Keep it deterministic — assert the invariants, never a specific winner.

### 3.9 Two candidate defects — name them, do not silently fix them

1. If §3.6 finds that deciding twice leaves two rows, that is a real defect (the reviewer's history
   would double-count). **Do not change `review.py` in this step.** Write the test to document the
   current behaviour, mark it `@unittest.expectedFailure` with a comment naming the finding, and
   report it in the final message so the next step can decide.
2. If §3.1 finds that a failed `refresh()` wipes the previous good snapshot, same treatment.

An `expectedFailure` is honest documentation; a test that asserts the wrong thing to look green is
not. Report any `expectedFailure` you add, with the reason, in the final message.

---

## 4. Tests to add — summary

| file | class | pins |
|---|---|---|
| `tests/conftest.py` (new) | session fixture | C8: no temp dir outlives the run |
| `tests/test_error_contract.py` (new) | `ServiceGuardTests` | §2.1 — 7 sites (6 new, 1 tighten) |
| | `ReviewGuardTests` | §2.2 — 11 sites (6 new, 5 tighten) |
| | `SyncGuardTests` | §2.3 — 10 sites (7 new tests, 3 tighten-in-place) |
| | `DeployGuardTests` | §2.4 — 6 sites (4 new tests, 2 tighten-in-place) |
| `tests/test_connector_contract.py` (extend) | `ConnectorMessageTests` | §2.5 — 13 messages |
| | `HttpStatusPassthroughTests` | §2.6 — 412 quirk, status carry-through, transport failures |
| `tests/test_edge_scenarios.py` (new) | `RefreshFailureTests`, `OverviewStatusTests`, `PartialMatchingTests`, `EmptyInventoryTests`, `EmptyAuditTests`, `DecisionUpsertTests`, `BlackDuckDedupTests`, `HttpRaceTests` | §3.1–§3.8 |

Roughly **45 new tests**, all `unittest.TestCase`, plus about a dozen assertions tightened inside
existing tests. Expected new total: **~268 passed**. Coverage will
rise from 88%; report the new number (the gate stays at 75 and must not be raised in this step).

---

## 5. Commit split

1. `Stop the test suite leaking temp dirs and sqlite connections (C8)` — `tests/helpers.py`,
   `tests/conftest.py`, `tests/test_e2e.py`, `tests/test_read_scale.py`.
2. `Pin the validation guards that nothing exercised (C5: service, review, sync, deploy)` —
   `tests/test_error_contract.py`.
3. `Pin the connector failure messages and status passthrough (C5: connectors)` —
   `tests/test_connector_contract.py`.
4. `Cover the failure and empty paths nobody reached (C7)` — `tests/test_edge_scenarios.py`.

Each commit green on its own. No `clreview/` change is expected; if one turns out to be unavoidable,
it goes in its own commit, names the defect, and says why it could not wait.

---

## 6. Verification

```bash
uv run pytest -q --ignore=adws                                   # green, coverage gate green
uv run ruff check clreview tests                                 # exit 0
uv run pytest tests/test_web_smoke.py -q --ignore=adws           # browser smoke green
uv run python -m unittest discover -s tests -p 'test_*.py' -q    # same count as pytest
# C8 gate
rm -rf /tmp/clreview-test-* && uv run pytest -q --ignore=adws && ls -d /tmp/clreview-test-* 2>/dev/null | wc -l
# red proof
cp -a . /tmp/s9red && cd /tmp/s9red   # then delete the guards the new tests pin and re-run them
```

Report, as numbers:

* new test count (baseline **223**) and new total coverage (baseline **88%**),
* raise messages with no test before (**49**) and after (target ≈ 0; list and justify every
  remaining one),
* temp dirs leaked per suite run before (**70**) and after (**0**), twice in a row,
* warning count before (**57**) and after,
* the red-proof failure count per new test file (e.g. "with the guards removed:
  `test_error_contract.py` 29 failed"), and
* any `expectedFailure` added under §3.9, with the defect it documents.

---

## 7. Out of scope — do not touch

* A, B, D, E, F findings; C1–C4 and C6 (already landed).
* Property-based testing, fuzzing, hypothesis, mutation testing, coverage-driven filler tests.
* New dependencies of any kind.
* Raising `--cov-fail-under`, or adding `pytest` plugins.
* Refactoring `clreview/` to make tests easier — the guards are already callable; if one is not,
  say so rather than restructuring.
* Tests that restate an implementation detail (asserting a private helper's return shape, or that a
  mock was called, when the user-visible outcome is already covered).
* The 45 raise messages already pinned, and the two `http.py` sites whose only gap is a short
  fragment — tightening them is optional, not required.
* `tests/web_smoke.mjs` and the browser smoke test, beyond keeping it green.
