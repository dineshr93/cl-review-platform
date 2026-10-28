# Step 8 — Maintainability: D1, D2, D3, D4, D5

Session `6f231271`. Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`.
HEAD at planning time: `b46836e` (steps 1–7 landed). A, B, C, E, F findings are out of scope.

**Baseline measured at HEAD on this machine:** `uv run pytest -q --ignore=adws` → **208 passed,
coverage 86.30%**, `ruff check clreview tests` exit 0, `clreview/` + `tests/` = **9,122 lines**.
Every line number below was re-read at this HEAD; the scout's line numbers had drifted (steps 1–7
moved things) and two of its bullets are now stale — called out inline.

Order of work: **D1 (delete) → D2 (dedupe) → D5 (cheap shape nits) → D3 (refactors) → D4 (frontend)**.
Deletion first: it shrinks everything that follows, and D2's biggest candidate disappears once D1
is done.

---

## 0. Ground rules

* Runner is `uv`. Gates: `uv run pytest -q --ignore=adws` (coverage gate `--cov-fail-under=75`),
  `uv run ruff check clreview tests`, and the browser smoke test
  `uv run pytest tests/test_web_smoke.py -q --ignore=adws`.
* Single-file pytest runs need `--no-cov` (`--cov` lives in `addopts`).
* No new dependencies. No framework migration. No reformatting of untouched code. No rename that
  only satisfies taste.
* **Every deletion carries evidence** (the grep output that shows nothing reaches it), not a hunch.
* **Every dedup names the copies, says which behaviour won and why, and pins the survivor with a
  test.** A candidate that fails that test is *declined in writing*, not silently skipped.
* No route, status code or response shape changes silently. §7 lists every user-visible text change
  this plan makes, each pinned by a test.

---

## 1. D1 — dead code shipped in the package

Evidence command (run it, keep the output in the commit body):

```bash
for n in find_purl summarize_discrepancies is_generic_text classify_text_kind to_dict \
         _inventory_for_matching "state.detail"; do
  echo "== $n"; grep -rn --include='*.py' --include='*.js' --exclude-dir=.git \
      --exclude-dir=__pycache__ --exclude-dir=.venv "$n" clreview tests
done
```

| item | location | evidence at HEAD | action |
|---|---|---|---|
| `find_purl` | `clreview/domain.py:227` | one hit: the `def` itself, in all of `clreview/` + `tests/` | **delete** (~15 lines) |
| `summarize_discrepancies` | `clreview/comparison.py:234` | 2 hits: the `def` + its `__all__` string | **delete** + drop the `__all__` entry |
| `is_generic_text` | `clreview/comparison.py:251` | 2 hits: `def` + `__all__` | **delete** + drop the `__all__` entry |
| `classify_text_kind` | `clreview/comparison.py:258` | 2 hits: `def` + `__all__` | **delete** + drop the `__all__` entry |
| `HttpError.to_dict` | `clreview/connectors/http.py:27` | 1 hit: the `def` | **delete** (3 lines) |
| `Service._inventory_for_matching` | `clreview/service.py:317` | 1 hit: the `def`; `_run_matching` calls `side_components_with_observations` + `_to_component_data` directly (`service.py:379-380`) | **delete** (3 lines) |
| `CL.state.detail = d;` | `clreview/web/workspace.js:28` | the only `state.detail` occurrence in either JS file — written, never read | **delete the line** (keep the `d` local, it is used below) |

**Stale scout bullet — do not act on it:** D1 lists `http.put_json` as unused. It is used:
`clreview/connectors/blackduck.py:250` (`status, parsed = put_json(key, body, ...)`) since the A1
fix in `7fd997a`, and `tests/test_connectors.py:133,141` patches it. **`put_json` stays.**

**Not dead, do not touch:** `api.Handler.version_string` / `log_message` (BaseHTTPRequestHandler
hooks), `base.ProjectLevelConnector` / `SourceLevelConnector` (the Protocols are the connector
contract), `comparison.norm_copyright` (live), `sync._pl_component` (used at `sync.py:214`).

`comparison.__all__` is the only "public API" concern the scout raised. Checked: no README, doc,
notebook or test imports those three names; the package is not published as a library (it ships a
server console script, `pyproject.toml:32`). Deletion is the right call, and it is the cheapest
diff in this whole step.

---

## 2. D2 — duplicated logic

Four candidates. Two are real and get done, one is already fixed, one is declined with evidence.

### 2.1 DONE — the `UnsupportedOperation` refusals (3 connectors, 12 stub methods)

Current copies (each method is a one-line `raise`):

| method | `fossology.py:291-302` | `scancode.py:74-84` | `fossid.py:47-57` |
|---|---|---|---|
| `read_component` | `"FOSSology cannot be a write target; the platform never reads PL values from it"` | `"ScanCode is an evidence source; the platform never reads PL values from it"` | `"FossID is a comparison service; it is never a write target"` |
| `update_licenses` | `"FOSSology is a source-level tool; synchronization flows SL -> PL only"` | `"ScanCode is an evidence source; synchronization flows SL -> PL only"` | `"FossID is a comparison service; synchronization flows SL -> PL only"` |
| `update_copyrights` | same as `update_licenses` | same | same |
| `update_license_text` | same | same | same |

Twelve methods, four message templates, wording already drifted between the three copies. The only
existing test is `tests/test_connectors.py:318` `test_fossology_write_operations_are_unsupported`,
which calls FOSSology's three `update_*` methods and asserts the exception **type** only — no test
covers `read_component` on any connector, nor ScanCode's or FossID's writes, nor any message text.

**Fix:** add to `clreview/connectors/base.py` (after `credential()`, before the registry):

```python
class NoWriteEndpoints:
    """Shared refusal for a tool that documents no write API.

    A connector sets two phrases and inherits the four methods; the messages stay the
    user-facing contract they always were, asserted in tests/test_connectors.py.
    """

    evidence_role: str = "this tool"          # "FOSSology is a source-level tool"
    write_target_denial: str = ""            # "" -> derive from evidence_role

    def read_component(self, key: str) -> dict[str, Any]:
        raise UnsupportedOperation(
            f"{self.write_target_denial or self.evidence_role}; "
            "the platform never reads PL values from it")

    def update_licenses(self, key, entries):        # noqa: D102 - four refusals, one shape
        raise UnsupportedOperation(f"{self.evidence_role}; synchronization flows SL -> PL only")

    def update_copyrights(self, key, notices):
        raise UnsupportedOperation(f"{self.evidence_role}; synchronization flows SL -> PL only")

    def update_license_text(self, key, text, file_name=None):
        raise UnsupportedOperation(f"{self.evidence_role}; synchronization flows SL -> PL only")
```

`UnsupportedOperation` lives in `clreview/connectors/http.py`; import it into `base.py` (no cycle:
`http.py` imports nothing from the package). The connectors are plain classes — `base.py` defines
`Protocol`s, not ABCs, so adding a mixin cannot break a method-resolution order.

* `FossologyConnector(NoWriteEndpoints, SourceLevelConnector)` with
  `evidence_role = "FOSSology is a source-level tool"`, `write_target_denial = "FOSSology cannot be a write target"`.
* `ScanCodeConnector(NoWriteEndpoints, SourceLevelConnector)` with `evidence_role = "ScanCode is an evidence source"`.
* `FossidConnector(NoWriteEndpoints, SourceLevelConnector)` with
  `evidence_role = "FossID is a comparison service"`, `write_target_denial = "FossID is a comparison service; it is never a write target"`.

**Which behaviour won:** the `synchronization flows SL -> PL only` phrasing is kept verbatim in all
three (it is the sentence an operator needs). `read_component` unifies on
`"<denial>; the platform never reads PL values from it"`; ScanCode's message is unchanged,
FOSSology's is unchanged, and **FossID's changes** from
`"FossID is a comparison service; it is never a write target"` to
`"FossID is a comparison service; it is never a write target; the platform never reads PL values from it"`.
That is the one user-visible text change in this section; it is listed in §7 and pinned by
`test_unsupported_write_messages_are_unified`.

Delete the 12 stub methods (~40 lines). `mock.py`'s two stubs (`MockSLConnector.update_licenses` /
`update_license_text`, "mock PL cannot …") are **out of scope** — the finding names three
connectors, and a test double's message is not a user-facing contract.

**Test:** `tests/test_connectors.py::UnsupportedMessageTests` — for each of the three connectors,
call all four methods and assert the exact message string. This is the pin for the surviving
behaviour; without it the dedup is unverified.

### 2.2 ALREADY FIXED — license-expression tokenizing

The scout's second bullet ("tokenizing duplicated at `scancode.py:92` and `:110`") was overtaken by
A7. `split_license_expression()` now lives at `clreview/connectors/scancode.py:14-24` and **both**
call sites use it: `_package` (`:107`) and `_grouped_files` (`:124`). FOSSology never tokenizes an
expression (it reads the `findings.scanner` list). Evidence:

```bash
grep -rn "split_license_expression" clreview tests
# def + 2 calls + the test that pins it (tests/test_connectors.py::test_license_expression_splitting)
```

**No action.** Do not "re-deduplicate" it. (If anything, the helper could move to `domain.py` — that
is a taste rename, explicitly out of scope.)

### 2.3 DECLINED with evidence — `fossology._group` vs `scancode._grouped_files`

The scout claims these have "already drifted". They have not, in any way that changes output.
Both rules were extracted and compared over every input shape (this was run, not reasoned):

| paths in the scan | FOSSology groups | ScanCode groups | same partition? |
|---|---|---|---|
| none | — | — | yes |
| root files only | `UPLOAD` | `root` | **yes** |
| one top dir | `a` | `a` | yes |
| one top dir + root files | `a` | `a` | yes |
| two top dirs | `a`, `b` | `a`, `b` | yes |
| two top dirs + root file | `UPLOAD`, `a`, `b` | `root`, `a`, `b` | **yes** |
| deep single dir | `a` | `a` | yes |
| one top, nested | `a` | `a` | yes |

(`fossology.py:329-344` vs `scancode.py:118-124`.) The only difference is the **label** of the
fallback group — `upload_name` vs `"root"` — and those two *must* differ: FOSSology names the
component after the upload, ScanCode has no upload name. The A2 bug the scout mentions was a FOSS
copy of the single-component rule and was fixed in `7fd997a`; ScanCode's rule never had it.

What a shared `group_files()` would actually need:

* an **input adapter** per connector — FOSSology reads `filePath` / `findings.scanner` /
  `findings.copyright`; ScanCode reads `path` / `license_detections[].license_expression` /
  `copyrights[].value` / `text`;
* an **output adapter** — FOSSology returns `dict[key → {name, paths, licenses, copyrights}]` and
  keeps the per-path provenance maps that `_upload_inventory` needs for `evidence_paths`; ScanCode
  returns `list[ComponentData]` and additionally collects license-text bodies;
* a **parameter** for the fallback label, which is the only real difference in the partition;
* different **dedupe rules** downstream — FOSSology dedupes copyright texts through a `seen` set
  and merges the `/uploads/{id}/copyrights` aggregate (`fossology.py:240-253`), ScanCode does
  neither (`scancode.py:145-148`), and FOSSology fetches license text bodies over HTTP while
  ScanCode reads them from the file record (`scancode.py:134-136`).

Both sides are already pinned: `FossologyGroupingTests` (`tests/test_connectors.py:290`, including
the root-files-join-the-single-top-dir case that A2 fixed) and `ScanCodeTests` (`:481`).

That is more code than the ~25 lines it removes, on two working connectors, for zero behaviour
change. **Declined.** The plan records it so nobody spends a second pass on it.

### 2.4 DONE — the frontend duplications

**a) `loadScans` / `loadDeployFolders` are the same function.** `app.js:352-359` and
`app.js:641-653` differ only in the select id, the query key (`scan_id` vs `folder_id`), the
placeholder text and the toast text. Merge into one:

```javascript
async function loadScanOptions(selectId, connectionId, keep, placeholder) { ... }
```

`loadScans` and `loadDeployFolders` become one-line calls. The deploy placeholder
`'— pick a folder (Unpacked is the default) —'` is passed in, so no text changes.

**b) `loadProjects` / `loadDeployProjects` are NOT a copy — do not merge them.**
`app.js:336-350` calls `/api/connections/{id}/projects` and reads `p.id` / `p.name`;
`app.js:620-639` calls `/api/deploy` with a `dry_run` body and reads `r.blackduck.project_id` /
`r.blackduck.project_name`, then chains `loadDeployProjects`. Merging them would need a
request-shape flag and would hide that one is discovery and the other is a deploy report.
**Declined**, with the field lists above as the evidence.

**c) The three-line discovery catch block is copied three times** — `app.js:349-350`, `:358-359`,
`:638-639` (only the noun differs). Extract:

```javascript
loadFailure: (what, e) => CL.toast(`${what} discovery failed: ${e.message}`, true)
```

**d) The six-button decision block is copied twice** — `workspace.js:100-106` and `:124-130` are
identical except the `data-*` attribute (`data-dec`/`data-key`/`data-action` vs
`data-suggestion`/`data-sdec`). Extract one builder:

```javascript
const actionButtons = (attrs, actions) => actions.map(([action, label, extra]) =>
  `<button type="button" class="small ${extra}" ${attrs}>${label}</button>`).join('');
```

Keep every emitted attribute and label byte-identical: `web_smoke.mjs` clicks
`[data-dec="accept_pl"][data-key="…"]` (`:456`) and `[data-sdec="accept"][data-suggestion="…"]`
(`:560`).

**e) `DEPLOY_LABEL` restates `DEPLOY_STATS`.** `app.js:511-514` maps
`created→'created'`, `existing→'already there'`, `failed→'failed'`; `app.js:508-510` already maps
the same keys to `'created'`, `'already there'`, `'failed'` — the two tables agree for all three
keys, so `DEPLOY_LABEL` is a pure duplicate. **Delete `DEPLOY_LABEL`** and read the label from
`DEPLOY_STATS[k].label` (add the label to that table's entries). No text changes.

---

## 3. D5 — backend shape nits (cheap, do before the refactors)

1. **Mid-file import.** `clreview/service.py:498` —
   `from .domain import CopyrightEntry, LicenseEntry, LicenseTextCandidate  # noqa: E402`.
   `domain.py` imports only stdlib (`domain.py:7-12`), so there is no cycle to hide from: the real
   cycle is `review.py` ↔ `service.py`, which this import is not part of. Move the three names into
   the top import block and delete the `# noqa`.
2. **`_inventory_for_matching`** — dead (§1). Deleting it also deletes the latent trap the scout
   noted (it rebuilt `ComponentData` through `_to_component_data`, which drops `write_key` and
   `source_files`). `_to_component_data` itself stays: `_run_matching` uses it at
   `service.py:379-380` and that path only feeds matching, which reads neither field.
3. **`mock.list_uploads` staleness.** `clreview/connectors/mock.py:239-240` is the only read in
   `MockSLConnector` that skips `self._maybe_reload()` (siblings call it at `:214`, `:219`, `:227`),
   so a fixture edit is invisible to folder listing until something else reloads. Add the call.
   The bare `int(folder_id)` stays a `ValueError` — that is the **correct** mapping (A4/C4 map
   `ValueError → 400`; `ConnectorError` would turn a bad id into a 502). Improve the message only:
   `raise ValueError(f"folder id must be a number, got {folder_id!r}") from None`.
4. **`import os` inside a function.** `clreview/deploy.py:109` (in a `finally:` after the id2url
   subprocess). `deploy.py` already imports `json`, `shutil`, `subprocess`, `tempfile` at the top
   (`:11-15`); move `os` there.

**Test:** `tests/test_connectors.py::MockReloadTests.test_list_uploads_sees_a_rewritten_fixture`
(rewrite the SL fixture's uploads, call `list_uploads`, assert the new upload appears) and
`test_list_uploads_rejects_a_non_numeric_folder` (asserts `ValueError` and the message).

---

## 4. D3 — oversized functions

### 4.1 `Api.handle` → a route table (`clreview/api.py:51-166`, 116 lines)

Today it is one if-chain that re-`re.fullmatch`es a pattern per route and then branches on method
and on which optional group matched. Replace with a module-level table and one small method per
route:

```python
ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"/api/connections/(\d+)/test"), "_test_connection"),
    ("GET",  re.compile(r"/api/connections/(\d+)/projects"), "_list_projects"),
    ("GET",  re.compile(r"/api/connections/(\d+)/scans"), "_list_scans"),
    ("GET",  re.compile(r"/api/connections/(\d+)/folders"), "_list_folders"),
    ("POST", re.compile(r"/api/mappings/(\d+)/refresh"), "_refresh"),
    ("GET",  re.compile(r"/api/mappings/(\d+)/overview"), "_overview"),
    ("GET",  re.compile(r"/api/mappings/(\d+)/components"), "_components"),
    ("POST", re.compile(r"/api/suggestions/(\d+)/decision"), "_decide_suggestion"),
    ("GET",  re.compile(r"/api/components/(\d+)"), "_detail"),
    ("POST", re.compile(r"/api/components/(\d+)/decisions"), "_decide"),
    ("POST", re.compile(r"/api/components/(\d+)/notes"), "_add_note"),
    ("POST", re.compile(r"/api/components/(\d+)/review"), "_set_review_state"),
    ("POST", re.compile(r"/api/components/(\d+)/confirm"), "_confirm_match"),
    ("POST", re.compile(r"/api/components/(\d+)/reassign"), "_reassign"),
    ("POST", re.compile(r"/api/components/(\d+)/reject"), "_reject_match"),
    ("POST", re.compile(r"/api/components/(\d+)/suggestions"), "_add_suggestion"),
    ("GET",  re.compile(r"/api/health"), "_health"),
    ("GET",  re.compile(r"/api/version"), "_version"),
    ("GET",  re.compile(r"/api/tools"), "_tools"),
    ("GET",  re.compile(r"/api/connections"), "_list_connections"),
    ("POST", re.compile(r"/api/connections"), "_add_connection"),
    ("GET",  re.compile(r"/api/mappings"), "_list_mappings"),
    ("POST", re.compile(r"/api/mappings"), "_create_mapping"),
    ("GET",  re.compile(r"/api/mappings/(\d+)"), "_mapping"),
    ("POST", re.compile(r"/api/deploy"), "_deploy"),
    ("POST", re.compile(r"/api/sync/preview"), "_sync_preview"),
    ("POST", re.compile(r"/api/sync/jobs"), "_create_job"),
    ("GET",  re.compile(r"/api/sync/jobs"), "_list_jobs"),
    ("GET",  re.compile(r"/api/sync/jobs/(\d+)"), "_job"),
    ("POST", re.compile(r"/api/sync/jobs/(\d+)/retry"), "_retry_job"),
    ("GET",  re.compile(r"/api/audit"), "_audit"),
)

def handle(self, method, path, query, body, reviewer):
    def q(name, default=""):
        return (query.get(name) or [default])[0]
    for route_method, pattern, handler in ROUTES:
        if route_method != method:
            continue
        m = pattern.fullmatch(path)
        if m:
            return getattr(self, handler)(m, body, reviewer, q)
    raise KeyError(f"no route for {method} {path}")
```

Each handler is `def _detail(self, m, body, reviewer, q): return 200, self.review.detail(int(m.group(1)))`.

**Behaviour that must not move:**

* `fullmatch` (not `match`) keeps the ordering hazard away: `/api/mappings/5/components` cannot be
  stolen by `/api/mappings/(\d+)`.
* A path that matches a pattern but no branch today (`GET /api/components/5/decisions`,
  `GET /api/mappings/5/refresh`, `POST /api/components/5`) must still fall through to
  `KeyError("no route for …")` → 404. The table gives this for free because there is no entry.
* The `KeyError` text stays exactly `f"no route for {method} {path}"` — `tests/test_e2e.py:254`
  (`test_unknown_route_is_404`) and `:357` (404 body is a plain message) pin it.
* `min(200, …)` on `limit` for components and `min(500, …)` on `limit` for audit are copied into
  their handlers unchanged.
* The `from .deploy import deploy` local import stays local (it keeps `deploy`'s subprocess module
  off the import path of every request).

**Tests:** existing `tests/test_e2e.py` covers the routes end to end. Add
`tests/test_e2e.py::RouteTableTests.test_every_route_entry_is_wired` — iterate `ROUTES`, assert the
handler exists on `Api` and is callable, and assert no duplicate `(method, pattern.pattern)` pair;
plus `test_method_mismatch_is_404` for `GET /api/components/5/decisions` and
`POST /api/components/5`.

### 4.2 `ReviewService.decide` → per-field resolvers (`clreview/review.py:357-437`, 81 lines)

Split the value-resolution branching into three module-private helpers that keep the **exact**
`ValueError` messages:

* `_copyright_value(cmp, key, action, value)` → the copyright branch (`:368-390`),
* `_license_value(cmp, key, action, value)` → the license branch (`:391-403`),
* `_license_text_value(cmp, key, action, value)` → the text branch (`:404-412`).

`decide` becomes: validate field/action → `match`/`comparison` → `value = resolver(...)` →
`note_only` guard → the existing `with self.db.transaction():` block, untouched.

**None of these eight messages is pinned by any test today** (verified: grepping all eight strings
across `tests/` returns nothing). So the split must add
`tests/test_review.py::DecisionValidationTests` with one assertion per message:

```
approve_sl requires an SL copyright entry key
accept_pl requires a PL copyright entry key
correct requires approved_value                      (copyright and license paths)
entry_key does not exist in either side              (copyright and license paths)
approve_sl requires an SL license entry key
accept_pl requires a PL license entry key
license text must come from an identified SL source-file candidate
note_only requires a note
```

Each case asserts `ValueError` **and** the message, and each is reached through the HTTP route at
least once (`POST /api/components/{id}/decisions`) so a future refactor cannot move a message into
a place the API never reaches.

### 4.3 `ReviewService.detail` → named views (`clreview/review.py:253-331`, 78 lines)

Extract, behaviour-preserving, no response-shape change:

* `_decision_view(cmp, decided, rejected)` → `copyright_rows`, `license_rows`, `text_view`;
* `_detail_warnings(match, cmp, pl_comp, sl_comp)` → the five `warnings.append` rules;
* `_reassignment_candidates(match)` → the `SequenceMatcher` block (only called for
  `unmatched`/`rejected`, keep that guard where it is).

`detail` keeps its exact return dict. `tests/test_review.py` already pins the detail shape; add
`test_detail_warnings_are_unchanged` asserting the full warning list for a suggested match with one
side missing, so the extraction cannot drop a rule.

### 4.4 JS render functions: separate template from bind

The pattern already exists and is the model: `connectionsPanel(items)` returns a string
(`app.js:248-253`) and `bindOverview()` wires events (`app.js:255-291`). Apply it to the four
functions that mix the two:

| function | today | split into |
|---|---|---|
| `renderOverview` `app.js:192-243` (52) | body + `bindOverview` already separate | extract `overviewBody(ov)`; keep `bindOverview` |
| `renderQueue` `app.js:374-427` (54) | filters + table + pager + 6 bindings in one `innerHTML` write | `queueFilters(state)`, `queueRows(items)`, `bindQueue()` |
| `renderHistory` `app.js:430-482` (53) | rows + 4 bindings | `historyRows(jobs)`, `bindHistory()` |
| `renderDeploy` `app.js:563-618` (56) | report + form + 5 bindings | `deployBody(report)`, `bindDeploy()` |

`renderQueue` also gets D4's tbody/filter split (§5.7), so do them as one change.

**Hard constraint — the smoke test's DOM contract.** These ids/attributes are clicked or read by
`tests/web_smoke.mjs` and must survive every JS change in this step:
`#mapping`, `#reviewer`, `#nav-overview`, `#nav-queue`, `#nav-history`, `#nav-deploy`,
`#nav-connections`, `#overview`, `#overview-refresh`, `#overview-sync-all`, `#queue-q`,
`#queue-status`, `#queue-refresh`, `#queue-count`, `#queue-body`, `#queue-pager`, `#history-body`,
`#deploy-project`, `#deploy-version`, `#deploy-scan`, `#deploy-folder`, `#deploy-refresh`,
`#deploy-report`, `#deploy-create`, `#connections`, `#component`, `#toast`, `#modal`,
`#modal-input`, `#modal-ok`, `#m-name`, `#m-tool`, `#m-base`, `#m-add`, `#m-pl`, `#m-sl`,
`#add-connection`, `#new-mapping-btn`, `#create-dry-run`, `#confirm-live`, `#progress`,
`#queue-pager button`, `data-dec`/`data-key`/`data-action`, `data-suggestion`/`data-sdec`,
`data-job`, `data-x`, `data-k`, and the `clickable` class. Read-only assertions: `#queue-count`
text (`:505`), `#queue-q` **value** after a page turn (`:506`), `#queue-body` text,
`#deploy-report` text, `#toast` text, the hidden-class state of `#modal` / `#progress` / `#toast`,
and `h1` text. So the search input must keep its value across a table re-render, and the pager must
stay a `<button>` inside `#queue-pager`.

---

## 5. D4 — frontend correctness and maintainability

### 5.1 Unescaped server data (five `CL.dash` sites in `workspace.js`, one raw field in `app.js`)

`CL.dash` (`workspace.js:6`) stringifies without escaping; `CL.esc` (`app.js:10`) escapes
`& < > " '`. These five interpolate scanner-controlled values straight into `innerHTML`:

```
workspace.js:49   <dd class="small">${CL.dash(pl && pl.purl)}</dd>
workspace.js:50   <dd class="small">${CL.dash(sl && sl.purl)}</dd>
workspace.js:51   <dd class="small num">${CL.dash(pl && (pl.source_version || '').slice(0, 16))}</dd>
workspace.js:52   <dd class="small num">${CL.dash(sl && (sl.source_version || '').slice(0, 16))}</dd>
workspace.js:185  <td class="num">${CL.dash(c.sl_version)}</td>
```

Six of the eleven `CL.dash` call sites are already correct (`app.js:236`, `:446`, `:490` wrap it in
`CL.esc`), which makes the five above an inconsistency rather than a convention. One more raw
server string reaches `innerHTML` and must be escaped in the same commit: `app.js:493` interpolates
`${i.api_status || ''}` (a connector response field) into the job-item table unescaped, while every
other cell on that row uses `CL.esc` or `CL.badge`.

Fix: wrap the five in `CL.esc(...)` — `CL.esc(CL.dash(x))`, the same composition `app.js:490`
already uses — and `CL.esc(i.api_status || '')` at `:493`. **Do not escape inside `dash`**: `app.js:469`, `:475`, `:500` pass `CL.dash(...)` to
`CL.toast`, which writes `textContent` (`app.js:59`) and would show a literal `&amp;`.

**Test:** new `tests/test_web_assets.py::EscapingTests.test_no_unescaped_dash_in_a_template` —
read `clreview/web/*.js` and assert every `CL.dash(` occurrence is immediately preceded by
`CL.esc(` or is an argument to `CL.toast(`. This is a static check: deterministic, no browser, and
it fails the moment someone adds a sixth site. Keep it a text scan, not a JS parser (no new
dependency), and keep its scope to `CL.dash` plus `i.api_status` — the raw numeric/id interpolations
the audit also listed (`app.js:155`, `:378`, `:405`, …) are server-generated integers and are out of
scope here.

### 5.2 Render race — a stale fetch can fill a newer view

`app.js:762` (`window.addEventListener('hashchange', () => CL.route())`) starts a new `route()`
without cancelling the previous one, `app.js:735-741` (the mapping selector) calls `CL.route()`
fire-and-forget, and `CL.api` (`app.js:34-53`) has no `AbortController` — grep finds zero in the
whole frontend — so a slow response for the old view can overwrite the new one. `CL._render`
(`app.js:171-191`) awaits per-view fetches with no token or lock.

Fix (≈10 lines, no framework): a monotonic token in `CL.route()`.

```javascript
CL.route = async function () {
  const token = ++CL.state.routeToken;
  ...
  const data = await CL.api(...);
  if (token !== CL.state.routeToken) return;   // a newer navigation won; drop this render
  ...
};
```

Bail after **every** `await` in `CL._render`. Declare both counters in `CL.state` (`app.js:5`) —
`inflight` is created lazily at `app.js:38` today and `routeToken` would otherwise be too. Do not
add `AbortController`: the token makes the stale render impossible, which is the actual bug, and
aborting would turn a discarded response into a thrown `AbortError` at fifteen call sites.

**Test:** in `web_smoke.mjs`, navigate `#/queue` → `#/overview` back-to-back with `untilApi`, then
assert the visible `h1` is the overview's and `location.hash` agrees. Add it as a numbered check so
a regression prints which check failed.

### 5.3 A dropped click gives no feedback

`app.js:115` — `if (CL.state.inflight) return undefined;` inside `CL.run(fn)` (note: **`CL.run`
takes only the function**, `app.js:113`; there is no label argument): a click during any in-flight
request does nothing at all. `body.busy` already shows a progress bar and `cursor: progress`
(`style.css:52-54`), but the button still looks live.

Fix (1 line, no CSS risk): toast when a click is dropped.

```javascript
if (CL.state.inflight) { CL.toast('still working — one moment'); return undefined; }
```

Do **not** add `body.busy button { pointer-events: none }`: the toast dismiss button, the modal
cancel and the reviewer-name input all live outside `#main` and must stay clickable, and a
pointer-events rule is exactly the kind of thing that makes the smoke test flaky.

**Test:** in `web_smoke.mjs`, evaluate `CL.state.inflight = 1` then call
`CL.run(() => Promise.resolve())` and assert `#toast` text contains "still"; reset
`CL.state.inflight = 0` afterwards. Deterministic — no timing race.

### 5.4 Unhandled rejections

`app.js:118` rethrows after toasting, and **fifteen** call sites use `onclick = () => CL.run(...)`
(`app.js:295`, `:303`, `:346`, `:356`, `:424`, `:425`, `:479`, `:534`, `:616`, `:627`, `:636`,
`:652`, `:681`, `:704`, plus `workspace.js:215`) — every failed action logs an unhandled rejection.
`app.js:636` reaches it a second way: it calls `loadDeployProjects()`, which ends in a `CL.run`.

The rethrow must stay: `await CL.run(...)` callers depend on it to skip their success toast
(`app.js:295-297` toasts "connection added" only if the run resolved). Fix at the call sites with a
fire-and-forget helper:

```javascript
CL.click = (fn) => { CL.run(fn).catch(() => {}); };
```

Replace the fifteen `onclick = () => CL.run(...)` sites with `onclick = () => CL.click(...)`. Leave
the `await CL.run(...)` sites alone — `app.js:295`, `:303`, `:346`, `:356` are awaited inside async
handlers and depend on the rethrow to skip their success toast.

Separately, `app.js:480` calls `CL.showJob(job.id)` outside `CL.run` — no busy state, no toast on
failure, and its rejection is unhandled. Wrap it: `CL.click(() => CL.showJob(job.id))`.

**Test:** `web_smoke.mjs` gains a check that `window.CL.click` exists and that a deliberately
failing `CL.click` leaves no unhandled rejection — install
`window.addEventListener('unhandledrejection', e => window.__rej.push(e.reason))` at the top of the
script and assert `window.__rej` is empty at the end of the run. That check also catches any future
regression in the other nine call sites.

### 5.5 Serial fetches that can be parallel

* `app.js:433-434` — `renderHistory` awaits the jobs list, then the preview. They are independent:
  `const [jobs, preview] = await Promise.all([...])`.
* `app.js:203` then `:240-241` — `renderOverview` awaits the overview, then the connections panel
  (which awaits `/api/connections` + `/api/tools`). Fetch overview, connections and tools with one
  `Promise.all`, then render.

Keep the render order identical (overview body first, panel after) so the smoke test's `until`
conditions still hold.

### 5.6 A11y

* **History job rows are mouse-only.** `app.js:446` emits
  `<tr class="clickable" data-job="${j.id}">` and `:480` attaches a click handler; the route
  `#/history/job/:id` already exists (`app.js:178-181`) and `app.js:236` already links job ids in
  the overview panel. Fix: make the id cell `<td><a href="#/history/job/${j.id}">#${j.id}</a></td>`
  and keep the row click for mouse users. Keyboard users get the link; no `tabindex` hack needed.
* **Queue re-render drops focus to `<body>`.** `app.js:407-414` re-renders on every filter, page or
  clear. `CL.focusKey` / `CL.restoreFocus` already exist (`workspace.js:7-16`, used at `:96-98`);
  capture the key before the re-render and restore after.
* **`#main` view swaps are silent to screen readers.** Add `aria-live="polite"` to `<main id="main">`
  (`index.html:36`). `#toast` already has `role=status aria-live=polite` (`index.html:44`), so do
  not add a second live region.

**Test:** `web_smoke.mjs` asserts the history row's anchor `href` starts with `#/history/job/`, and
that after a queue page turn `document.activeElement` is the pager control it clicked (not `body`).

### 5.7 Magic strings and the wholesale queue rebuild

* `'approved_for_sync'` appears at `app.js:12`, `:384` and `workspace.js:60`, `:160` (plus the
  related `'reviewed'` / `'in_progress'` literals). Add once, on the shared `CL` object
  (`app.js` defines `window.CL`, `workspace.js` reads it):

  ```javascript
  CL.reviewState = Object.freeze({ IN_PROGRESS: 'in_progress', REVIEWED: 'reviewed', APPROVED: 'approved_for_sync' });
  ```

  Replace the four occurrences. No wire value changes — the constants hold the same strings.
* **Queue rebuild.** `renderQueue` writes filters + table + pager in one `innerHTML`
  (`app.js:381`, `:405`). Split so a filter change or page turn rewrites only `#queue-body` and
  `#queue-pager`, and the filter block is rebuilt only when the mapping changes. This is the root
  cause of the focus loss in §5.6 and of the `#queue-q` value handling the smoke test depends on.

---

## 6. Tests to add (summary)

| file | test | pins |
|---|---|---|
| `tests/test_connectors.py` | `UnsupportedMessageTests` (3 connectors × 4 methods) | the unified refusal messages (§2.1) |
| `tests/test_connectors.py` | `MockReloadTests.test_list_uploads_sees_a_rewritten_fixture`, `test_list_uploads_rejects_a_non_numeric_folder` | D5.3 |
| `tests/test_e2e.py` | `RouteTableTests.test_every_route_entry_is_wired`, `test_method_mismatch_is_404` | D3.1 table completeness + fall-through 404 |
| `tests/test_review.py` | `DecisionValidationTests` (8 messages, service + HTTP) | D3.2 split is behaviour-preserving |
| `tests/test_review.py` | `test_detail_warnings_are_unchanged` | D3.3 |
| `tests/test_web_assets.py` (new) | `test_no_unescaped_dash_in_a_template` | D4.1, static, no browser |
| `tests/web_smoke.mjs` | route-token race, dropped-click toast, no unhandled rejections, history anchor href, focus after page turn | D4.2, D4.3, D4.4, D4.6, D4.7 |

`tests/test_web_assets.py` is plain Python reading files from `clreview/web/` — it runs in the
normal suite, needs no browser, and keeps the coverage denominator honest (it does not import
`clreview`, so it adds no uncovered lines).

---

## 7. User-visible changes (nothing else may change)

1. **FossID `read_component` refusal message** gains the shared second clause (§2.1). Pinned by
   `UnsupportedMessageTests`. Not a route, status or shape change: still `UnsupportedOperation` →
   409 with `{"error": "<message>"}`.
2. **A toast appears when a click is dropped** while a request is in flight (§5.3). New feedback, no
   removed feedback.
3. **History job ids become links** (§5.6). The URL they produce already existed.

No route, status code, request field or response field changes. If any of these turns out to be
impossible without changing a shape, stop and report instead of shipping the change.

---

## 8. Commit split

1. `Delete dead helpers shipped in the package (D1)` — `domain.py`, `comparison.py`,
   `connectors/http.py`, `service.py`, `web/workspace.js`.
2. `One refusal for "this tool has no write API", one scan-option loader, one button builder (D2)` —
   `connectors/base.py`, `connectors/fossology.py`, `connectors/scancode.py`, `connectors/fossid.py`,
   `web/app.js`, `web/workspace.js`, `tests/test_connectors.py`.
3. `Fix the four backend shape nits (D5)` — `service.py`, `connectors/mock.py`, `deploy.py`,
   `tests/test_connectors.py`.
4. `Route table for the API and named views for the review detail (D3)` — `api.py`, `review.py`,
   `tests/test_e2e.py`, `tests/test_review.py`.
5. `Split the JS renderers into template and bind (D3)` — `web/app.js`, `web/workspace.js`.
6. `Escape scanner data, stop stale renders, and make the frontend keyboard-usable (D4)` —
   `web/app.js`, `web/workspace.js`, `web/index.html`, `tests/test_web_assets.py`,
   `tests/web_smoke.mjs`.

Each commit must leave the suite green on its own.

---

## 9. Verification

```bash
uv run pytest -q --ignore=adws                                  # green, coverage gate green
uv run ruff check clreview tests                                # exit 0
uv run pytest tests/test_web_smoke.py -q --ignore=adws          # browser smoke green
uv run python -m unittest discover -s tests -p 'test_*.py' -q   # same count as pytest
```

Report, as numbers, in the final message:

* new total coverage (baseline **86.30%**),
* **net line delta**, measured as
  `git diff --numstat <step-8-start>..HEAD -- clreview tests | awk '{a+=$1;d+=$2} END {print a-d}'`
  and separately for `clreview/` alone (baseline tree: 9,122 lines across `clreview/` + `tests/`),
* the count of deletions attributable to D1 (expected ≈ 65 lines of source),
* the D2 stub count removed (12 methods) and the D3 function sizes after the split
  (`api.handle`, `review.decide`, `review.detail` — each expected under 45 lines).

Expected direction: source lines **down** (D1 + D2 stubs + `DEPLOY_LABEL` + `api.handle` if-chain
outweigh the new helpers), test lines **up**. If source lines go up, say why in the report.

---

## 10. Out of scope — do not touch

* A, B, C, E, F findings; B1/B2/B3; anything already landed in steps 1–7.
* The two declined candidates in §2.3 and §2.4b — the evidence is in the plan; do not re-litigate.
* No framework migration, no bundler, no ES modules, no test runner for the JS, no new dependency.
* No reformatting of untouched code; no line-length churn; no renames for taste
  (including moving `split_license_expression` to `domain.py`).
* `deploy.deploy` (130 lines), `sync._execute` (93), `fossology._upload_inventory` (82) and
  `matching.match_components` (128) are the biggest functions in the repo but **no finding names
  them** — leave them alone.
* `mock.py`'s own `UnsupportedOperation` stubs (test doubles, not a user-facing contract).
* No change to any route, status code or response shape (§7).
