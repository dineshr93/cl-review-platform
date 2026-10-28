# Plan — attack step 1: A1–A4 correctness fixes

Repo: `/home/dinesh/repos/foss_data_reviewer`. Baseline: `uv run pytest -q` → **103 passed in ~40 s** (measured before any edit). Every number below was reproduced by execution on this checkout.

Four independent small fixes. Land them in the order A1 → A2 → A3 → A4; each is one commit with its test. Nothing else changes.

All four defects were reproduced before planning:

```
A1  update_licenses -> ValueError: too many values to unpack (expected 2) | PUT already sent: [('PUT', .../components/c)]
A2  _group(["pkg-1.0/LICENSE","README"]) -> ['pkg-1.0','upload']   # README split into its own component
    _group(["README","pkg-1.0/LICENSE"]) -> ['pkg-1.0']            # same upload, different answer
A3  POST /api/connections config={"token":"SUPER-SECRET"} -> 200; DB config_json = {"token": "SUPER-SECRET", ...}
A4  POST /api/sync/preview {"mapping_id": null} -> 500 internal server error
    POST /api/deploy       {"pl_connection_id": null} -> 500
    POST /api/mappings     {"name": "x"} (no pl_connection_id) -> 404 'connection 0 not found'
```

---

## A1 — Black Duck license-name write crashes after the PUT is sent

**File:** `clreview/connectors/blackduck.py:229-233`

`request()` (`clreview/connectors/http.py:46-77`) returns a 3-tuple `(status, headers, data)`; the code unpacks it into two targets. The PUT is on the wire before the `ValueError` fires, and `ValueError` escapes the neighbouring `except HttpError`, so Black Duck may have accepted the change while clreview reports a 500 and the sync item is never updated.

`put_json` is already imported at `blackduck.py:21` and unused. Replace the call:

```python
        try:
            status, parsed = put_json(key, body, headers=self._headers(),
                                      timeout=self.timeout, retries=self.retries)
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc
        if status >= 400:
            raise ConnectorError(f"PUT {key} failed with {status}", status=status)
        return {"status": status, "body": parsed}
```

`put_json` returns `(status, parsed)` and does **not** raise on 4xx/5xx (unlike `get_json`), so the existing `if status >= 400` guard is what surfaces a rejected write — keep it. Do not add a media type or change `retries` here (A5 is out of scope).

**Test — `tests/test_connectors.py`, class `BlackDuckWriteTests`:**

`patch_api` currently patches `blackduck.request`, which `put_json` does **not** call (it calls `http.request` through its own module namespace), so add a `put_json` patch alongside the existing three:

```python
        def put_json(url, body, **kw):
            calls.append(("PUT", url, dict(kw.get("headers") or {}), body))
            return 200, {"ok": True}
```
and append `mock.patch.object(blackduck, "put_json", put_json)` to the returned patch list. Then:

```python
    def test_license_name_write_puts_and_returns_without_raising(self):
        def run(calls):
            result = self.conn().update_licenses(self.BOM, [{"name": "MIT"}])
            return result, calls[-1]
        result, (method, url, _headers, body) = self.with_api(run)
        self.assertEqual((method, url), ("PUT", self.BOM))
        self.assertEqual(body["licenses"], [{"licenseDisplay": "MIT", "license": self.LIC}])
        self.assertEqual(result["status"], 200)
```
The assertion that matters is that the call returns at all — before the fix this test dies with `ValueError`.

---

## A2 — FOSSology component grouping depends on set iteration order

**File:** `clreview/connectors/fossology.py:311-327` (`_group`)

`tops` is a `set` and `tops.pop()` runs **inside** the per-file loop (line 320). With one top-level dir plus root files, the first-iterated file consumes the popped key and later root files fall through to `upload_name` — the same upload groups differently between runs.

Fix it the way the sibling connector already does (`clreview/connectors/scancode.py:100-103`): compute the key once, before the loop.

```python
        single = len(tops) <= 1 and (root_files or len(tops) == 1)
        single_key = next(iter(tops)) if tops else upload_name
        groups: dict[str, dict[str, Any]] = {}
        for f in files:
            path = f.get("filePath") or ""
            if single and tops:
                key = single_key
                display = key
            elif "/" in path:
                ...unchanged...
```
Delete the `pop()`; touch nothing else in `_group`. Behaviour preserved for the three existing cases (multi-dir, single tree, root-only) — verified against the current tests:

| input | before | after |
|---|---|---|
| `pkg-a-1.0/src/x.c`, `pkg-a-1.0/LICENSE` | `['pkg-a-1.0']` | `['pkg-a-1.0']` |
| `LICENSE`, `COPYING` (no dir) | `['upload-7']` | `['upload-7']` |
| `pkg-a-1.0/LICENSE`, `pkg-b-2.0/LICENSE` | two groups | two groups |
| **`pkg-a-1.0/LICENSE`, `README`** | order-dependent | `['pkg-a-1.0']`, deterministic |

**Test — `tests/test_connectors.py`, class `FossologyGroupingTests`:**

```python
    def test_one_dir_plus_root_files_groups_deterministically(self):
        conn = self.conn()
        a = conn._group(self.files(["pkg-a-1.0/LICENSE", "README"]), "upload")
        b = conn._group(self.files(["README", "pkg-a-1.0/LICENSE"]), "upload")
        self.assertEqual(a, b)                       # dict equality: order-independent
        self.assertEqual(list(a), ["pkg-a-1.0"])
        self.assertEqual(sorted(a["pkg-a-1.0"]["paths"]), ["README", "pkg-a-1.0/LICENSE"])
```

---

## A3 — plaintext credentials persisted (chosen direction: **reject at the API boundary**)

**Decision:** keep `SECURITY.md:20-21` as the truth ("Tokens are never stored in the database") and make the code enforce it — `add_connection` **rejects** any `config` key that names a credential, so credentials reach a connector only through `auth_env` → environment variable. The alternative (silently strip the value before the INSERT) was rejected: it stores a connection that then fails at `test_connection` with a confusing "token missing", and it leaves `SECURITY.md` still overstating what the code does.

Evidence this is the low-blast-radius direction:
- Every documented example already uses env indirection — `config.example.json:9,17` and `docs/CONNECTORS.md:174,185` all use `"auth_env": {"token": "BLACKDUCK_TOKEN"}`; no doc, fixture, or test passes a literal token through `config` to `add_connection`.
- The web UI never sends a secret in `config` either: `clreview/web/app.js:282-295` builds `config` from `fixtures` only and puts the env-var *name* in `auth_env`.
- `SECRET_KEYS` (`clreview/service.py:15`) matches no config key the app itself uses (`fixtures`, `json_path`, `base_url`, `timeout`, `retries`, `agents`, `max_license_files`, `generic_text_limit`, `folders_as_scans`) — no false positives.
- `credential()`'s config fallback (`clreview/connectors/base.py:43-49`) **stays as-is.** Making it env-only would break four existing tests that construct connectors directly with `{"token": "t"}` (`tests/test_connectors.py:91`, `tests/test_deploy.py:235`, `:299`) via `_headers()` raising `ConnectorError`, and it guards nothing: a directly-constructed connector is never persisted. Do **not** edit `credential()` and do **not** change `tests/test_connectors.py:58-59`.

**File:** `clreview/service.py:43-56` (`add_connection`) — guard right after `cfg` is built, before the connector is built and before the INSERT:

```python
        cfg = dict(config or {})
        secrets = sorted(k for k in cfg if any(s in k.lower() for s in SECRET_KEYS))
        if secrets:
            raise ValueError(
                "connection config must not carry credentials: "
                f"{', '.join(secrets)}; name the environment variable in auth_env instead"
            )
```
`ValueError` already maps to HTTP 400 at `clreview/api.py:251`, so `POST /api/connections` gets a clear 400 with no DB row written. `sanitize_config` (read-path masking) stays — defence in depth, not the fix.

**File:** `SECURITY.md` (after line 21) — one sentence so the doc names the enforcement:

> Connection creation rejects a `config` value whose key names a credential (`token`, `password`, `username`, `api_key`, `secret`); name the environment variable in `auth_env` instead.

**File:** `clreview/connectors/base.py:46` — one docstring line on `credential()` recording the contract, so the fallback is not mistaken for a place to put a persisted secret: the `config` fallback serves directly-constructed connectors only; persisted connections never hold a secret (enforced in `service.add_connection`). No behaviour change.

**Test — `tests/test_connectors.py`, class `CredentialTests`** (module docstring already claims "credential hygiene"):

```python
    def test_literal_credential_never_reaches_persisted_state(self):
        from clreview.db import DB
        from clreview.service import Service
        db = DB(":memory:")
        svc = Service(db)
        with self.assertRaises(ValueError) as ctx:
            svc.add_connection("pl", "mock-pl", "leaky", "", {"token": "SUPER-SECRET"}, {})
        self.assertIn("auth_env", str(ctx.exception))
        with db.conn:
            rows = db.query("SELECT config_json FROM connections")
        self.assertNotIn("SUPER-SECRET", json.dumps([dict(r) for r in rows]))
        db.close()
```
(`json` and `unittest.mock` are already imported in that file; `DB`/`Service` are not — import them at module top, matching the file's existing import block.)

---

## A4 — malformed input returns 500 instead of 400

**File:** `clreview/api.py` — six bare `int(body.get(...))` sites: **118, 120, 129, 130, 136, 156**. `int(None)` raises `TypeError`, which only the catch-all `except Exception` (`api.py:259`) sees → 500. A missing id silently became `0` → a misleading 404 about `connection 0`.

Add one module-level helper next to `_safe_reviewer` (`api.py:185`):

```python
def _int(value: Any, name: str) -> int:
    """Required integer id from a JSON body: a bad or missing value is a client error."""
    if value is None or value == "":
        raise ValueError(f"{name} is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
```
`ValueError` → 400 via `api.py:251-252`. Apply it at all six sites with the field name:

| line | before | after |
|---|---|---|
| 118 | `int(body.get("pl_connection_id", 0))` | `_int(body.get("pl_connection_id"), "pl_connection_id")` |
| 120 | `int(body.get("sl_connection_id", 0))` | `_int(body.get("sl_connection_id"), "sl_connection_id")` |
| 129 | `int(body.get("pl_connection_id", 0))` | `_int(body.get("pl_connection_id"), "pl_connection_id")` |
| 130 | `int(body.get("sl_connection_id", 0))` | `_int(body.get("sl_connection_id"), "sl_connection_id")` |
| 136 | `int(body.get("mapping_id", 0))` | `mapping_id = _int(body.get("mapping_id"), "mapping_id")` |
| 156 | `int(body.get("mapping_id", 0))` | `_int(body.get("mapping_id"), "mapping_id")` |

Resulting status codes (all previously 500 or a misleading 404):

| request | before | after |
|---|---|---|
| `POST /api/sync/preview {"mapping_id": null}` | 500 | 400 `mapping_id is required` |
| `POST /api/sync/preview {}` | 200 with `mapping_id: 0` | 400 `mapping_id is required` |
| `POST /api/sync/jobs {"mapping_id": "abc"}` | 400 (raw `int()` message) | 400 `mapping_id must be an integer` |
| `POST /api/mappings {"name": "x"}` | 404 `'connection 0 not found'` | 400 `pl_connection_id is required` |
| `POST /api/deploy {"pl_connection_id": null}` | 500 | 400 `pl_connection_id is required` |

Leave query-string ints (`api.py:70-71`, `:159-160`, `:167-168`) alone — `int("abc")` already raises `ValueError` → 400. Out of scope: A11 (404 bodies carrying `KeyError` repr), A12 (chunked bodies), A13 (static prefix check).

**Test — `tests/test_e2e.py`, new class** (this is the file holding the live-server `Server` harness; the prompt's file list omitted it — no other test file can assert an HTTP status without rebuilding that harness):

```python
class BadInputTests(unittest.TestCase):
    """Malformed input is a client error (400), never a 500 or a misleading 404."""

    def setUp(self):
        self.db = DB(":memory:")
        self.mapping_id = seed_demo(self.db)["mapping_id"]
        self.server = Server(self.db)

    def tearDown(self):
        self.server.close()
        self.db.close()

    def test_null_and_missing_ids_are_400(self):
        for path, body in (("/api/sync/preview", {"mapping_id": None}),
                           ("/api/sync/preview", {}),
                           ("/api/sync/jobs", {"mapping_id": None, "dry_run": True}),
                           ("/api/deploy", {"pl_connection_id": None}),
                           ("/api/mappings", {"name": "x"})):
            status, payload = self.server.call("POST", path, body)
            self.assertEqual(status, 400, (path, body))
            self.assertNotEqual(status, 500)
            self.assertIn("required", payload["error"])

    def test_non_numeric_id_is_400_with_a_clean_message(self):
        status, payload = self.server.call("POST", "/api/sync/jobs", {"mapping_id": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "mapping_id must be an integer")
```
`Server` in `tests/test_e2e.py:17` is module-level, so a new class in that file reuses it as-is.

---

## Files touched

| file | change |
|---|---|
| `clreview/connectors/blackduck.py` | A1: `request` → `put_json` in `update_licenses` |
| `clreview/connectors/fossology.py` | A2: `single_key` computed once in `_group`, drop `tops.pop()` |
| `clreview/service.py` | A3: reject credential keys in `add_connection` |
| `clreview/connectors/base.py` | A3: `credential()` docstring only, no behaviour change |
| `SECURITY.md` | A3: one sentence naming the new enforcement |
| `clreview/api.py` | A4: `_int` helper + six call sites |
| `tests/test_connectors.py` | A1 write test + `put_json` patch, A2 grouping test, A3 credential test |
| `tests/test_e2e.py` | A4 `BadInputTests` |
| `tests/test_review.py`, `tests/test_suggestions.py` | expected **unchanged** — they are regression coverage for A4's service-layer callers; if either goes red, the A4 edit went too far |

## Verification

```bash
uv run pytest -q                 # expect 103 + 5 new = 108 passed, no skips beyond the browser smoke
uv run pytest -q tests/test_connectors.py tests/test_e2e.py -v
python -m unittest discover -s tests -t tests   # CI's runner must stay green too (C1 unfixed)
```
Then re-run the four probes to confirm each defect is gone:

```bash
uv run python /tmp/repro_a.py    # A1 prints "A1 result: {'status': 200, ...}"; A2 both orders -> ['pkg-1.0']
uv run python /tmp/repro_b.py    # A4 rows all 400; A3 add conn -> 400 and no SUPER-SECRET in config_json
```
(`/tmp/repro_a.py`, `/tmp/repro_b.py` are the scripts used for the baseline output above; recreate them from the "reproduced" block if they are gone. They are scratch files — do not commit them.)

## Out of scope — do not touch

A5–A13, every B/C/D/E/F finding (notably A5 POST retries, A6 pagination caps, C1 CI runner, C2 dev deps, D1 dead-code deletion — `put_json` stops being dead via A1, leave the rest), dependency changes, CI changes, refactors. No new dependencies. No reformatting of touched files beyond the edited lines.
