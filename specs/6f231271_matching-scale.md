# Plan — Attack step 4 (B1): make `refresh()` matching scale

**Repo:** `/home/dinesh/repos/foss_data_reviewer` · **HEAD:** `73a63a9` (step 3: indexes + foreign keys)
**Source of truth:** `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md` → finding **B1**
**Scope:** the three fixes the scout listed, in its order of value. Nothing else.

---

## 0. Baseline, measured on this machine

Benchmark: identical PL/SL inventory, 1 copyright + 1 license + 1 license text per component, temp DB in `/tmp`,
`_run_matching()` timed and every `query`/`one`/`execute` counted (script kept at `/tmp/bench_b1.py`; the shipped
test in §4 is the deterministic version of it).

| N components/side | `_run_matching` before | after | statements before | after |
|---|---|---|---|---|
| 200 | 0.04 s | **0.01 s** | 4,008 | **423** |
| 800 | 0.32 s | **0.05 s** | 16,008 | **1,630** |
| 1600 | 1.14 s | **0.10 s** | 32,008 | **3,244** |

Second refresh with identical data — the common case, a refresh that changed nothing — at N=1600:
**32,008 statements / 4.57 s before → 41 statements / 0.05 s after** (~90x). The old code recomputed all 1,600
matches because nothing told it otherwise; the new code recomputes none.

The scout measured 0.1 s / 2.0 s / 4.2 s on its own inventories; absolute seconds differ with fixture shape and
machine, the superlinear shape and the statement counts do not.

Where the 32,008 statements went at N=1600 (measured):

```
SELECT copyrights      6400   <- 2 sides x N, one query per component
SELECT licenses        6400
SELECT license_texts   6400
SELECT snapshots       3204   <- current_snapshot re-read inside every recompute
SELECT components      3202
SELECT matches         1602
INSERT matches         1600
```

After the fix, the same run is: `SELECT snapshots 8, components 6, copyrights 8, licenses 8, license_texts 8,
matches 2, decisions 4` (= **44 reads**) plus 1,600 `INSERT matches` and 1,600 `UPDATE matches` — the writes are
the work, not the reads.

Root cause, three separate N-fold amplifiers stacked:

1. `Service._inventory_for_matching` (`clreview/service.py:251-265`) calls `component_observations(row["id"])`
   per component → 3 queries per component per side.
2. `ReviewService.recompute()` (`clreview/review.py:97`) runs once per match, and each call re-reads the match,
   its decisions, both components and all their observations → `comparison()` → `_component()` →
   `component_by_key()` → `current_snapshot()`.
3. `current_snapshot()` is therefore re-queried thousands of times inside one call frame.

---

## 1. Fix 1 — batch the observation reads per snapshot (`clreview/service.py`)

Add a module-level chunk helper (after `SECRET_KEYS`, `clreview/service.py:26`):

```python
IN_CHUNK = 500  # stay well under SQLite's host-parameter limit when building IN (...) lists


def _chunks(items: list[Any]) -> list[list[Any]]:
    return [items[i:i + IN_CHUNK] for i in range(0, len(items), IN_CHUNK)]
```

Add `Service.components_observations` directly after `component_observations` (`clreview/service.py:183-188`).
Leave `component_observations` untouched — `clreview/review.py:66` and `clreview/api.py:244` still use it for
single-component reads.

```python
    def components_observations(self, snapshot_id: int) -> dict[int, dict[str, list[dict[str, Any]]]]:
        """Every observation row of a snapshot in one query per table, keyed by component id."""
        ids = [r["id"] for r in self.db.query("SELECT id FROM components WHERE snapshot_id=?", (snapshot_id,))]
        out: dict[int, dict[str, list[dict[str, Any]]]] = {
            i: {"copyrights": [], "licenses": [], "license_texts": []} for i in ids}
        for table in ("copyrights", "licenses", "license_texts"):
            for chunk in _chunks(ids):
                marks = ",".join("?" * len(chunk))
                rows = self.db.query(f"SELECT * FROM {table} WHERE component_id IN ({marks}) ORDER BY id", tuple(chunk))
                for row in rows:
                    out[row["component_id"]][table].append(row)
        return out
```

Replace `_inventory_for_matching` (`clreview/service.py:251-265`) with the two methods below. The
`ComponentData` construction moves verbatim into `_to_component_data` so there is exactly one place that builds it.

```python
    def side_components_with_observations(self, mapping_id: int, side: str) -> dict[str, dict[str, Any]]:
        """{external_id: component + observations} for a side, in four queries instead of 4N."""
        snap = self.current_snapshot(mapping_id, side)
        if not snap:
            return {}
        observations = self.components_observations(snap["id"])
        return {row["external_id"]: {"component": row, **observations[row["id"]]}
                for row in self.snapshot_components(snap["id"])}

    @staticmethod
    def _to_component_data(entry: dict[str, Any]) -> ComponentData:
        row = entry["component"]
        return ComponentData(
            external_id=row["external_id"], name=row["name"], version=row["version"], purl=row["purl"],
            metadata=json.loads(row["metadata_json"]), raw=json.loads(row["raw_json"]),
            copyrights=[_ce(o) for o in entry["copyrights"]],
            licenses=[_le(o) for o in entry["licenses"]],
            license_texts=[_te(o) for o in entry["license_texts"]],
        )

    def _inventory_for_matching(self, mapping_id: int, side: str) -> list[ComponentData]:
        return [self._to_component_data(e) for e in self.side_components_with_observations(mapping_id, side).values()]
```

`side_components_with_observations` returns the same shape `ReviewService._component` already returns
(`{"component": row, "copyrights": [...], "licenses": [...], "license_texts": [...]}`) — that is deliberate: the
inventory built here is the same object the review path consumes, which is what makes fix 3 possible. Ordering is
unchanged (`snapshot_components` still orders by `name, version`).

Add `snapshot_versions` next to it (used by fix 2):

```python
    def snapshot_versions(self, mapping_id: int, side: str, previous: bool = False) -> dict[str, str]:
        """{external_id: source_version} for this side's latest (or previous) successful snapshot."""
        snap = self.db.one(
            "SELECT id FROM snapshots WHERE mapping_id=? AND side=? AND status='ok' ORDER BY id DESC LIMIT 1 OFFSET ?",
            (mapping_id, side, 1 if previous else 0))
        if not snap:
            return {}
        rows = self.db.query("SELECT external_id, source_version FROM components WHERE snapshot_id=?", (snap["id"],))
        return {r["external_id"]: r["source_version"] for r in rows}
```

---

## 2. Fix 3 — one call frame, one set of reads (`clreview/review.py`)

The scout asked for a per-call-frame `current_snapshot` cache. The inventory from fix 1 already *is* that cache;
what was missing is a way to hand it to `recompute`. That is what these three small changes do — no new cache
class, no cross-request state.

Imports (`clreview/review.py:11` and `:17`):

```python
from dataclasses import asdict, dataclass
from .service import IN_CHUNK, Service, _ce, _le, _te, now
```

Add after `_decision_rows` (`clreview/review.py:20-21`):

```python
def decisions_by_match(db: DB, match_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Decisions for many matches in one query per chunk, in the order _decision_rows uses."""
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in match_ids}
    for chunk in [match_ids[i:i + IN_CHUNK] for i in range(0, len(match_ids), IN_CHUNK)]:
        marks = ",".join("?" * len(chunk))
        rows = db.query(f"SELECT * FROM decisions WHERE match_id IN ({marks}) ORDER BY field, entry_key", tuple(chunk))
        for row in rows:
            out[row["match_id"]].append(row)
    return out


@dataclass
class RecomputeContext:
    """Rows already read for one call frame, so recompute costs no reads of its own."""

    match: dict[str, Any]
    decisions: list[dict[str, Any]]
    inventory: dict[str, dict[str, dict[str, Any]]]
```

Thread it through the three read paths — every existing caller passes nothing, so behaviour is unchanged:

```python
    def _component(self, mapping_id: int, side: str, key: str, inventory: dict | None = None) -> dict[str, Any] | None:
        if not key:
            return None
        if inventory is not None:
            return inventory.get(side, {}).get(key)
        row = self.s.component_by_key(mapping_id, side, key)
        if not row:
            return None
        obs = self.s.component_observations(row["id"])
        return {"component": row, **obs}

    def comparison(self, match: dict[str, Any], inventory: dict | None = None) -> dict[str, Any]:
        mid = match["mapping_id"]
        pl = self._component(mid, "pl", match["pl_key"], inventory)
        sl = self._component(mid, "sl", match["sl_key"], inventory)
        ...unchanged...

    def recompute(self, match_id: int, context: "RecomputeContext | None" = None) -> dict[str, Any]:
        match = context.match if context else self.match(match_id)
        decisions = context.decisions if context else _decision_rows(self.db, match_id)
        cmp = self.comparison(match, inventory=context.inventory if context else None)
        ...unchanged from here down...
```

`ReviewService.detail`, `decide`, `confirm`, `reject`, `set_review_state` and the API keep calling these with no
context and read from the DB exactly as before.

---

## 3. Fix 2 — recompute only what moved (`clreview/service.py`)

Replace `_run_matching` (`clreview/service.py:267-299`) wholesale:

```python
    def _run_matching(self, mapping_id: int) -> dict[str, Any]:
        pl_snap = self.current_snapshot(mapping_id, "pl")
        sl_snap = self.current_snapshot(mapping_id, "sl")
        sides = {side: self.side_components_with_observations(mapping_id, side) for side in ("pl", "sl")}
        pl = [self._to_component_data(e) for e in sides["pl"].values()]
        sl = [self._to_component_data(e) for e in sides["sl"].values()]
        if not pl_snap or not sl_snap:
            return {"status": "partial", "pl": len(pl), "sl": len(sl), "reason": "one side has no successful snapshot"}
        results = match_components(pl, sl)
        existing = {r["pl_key"] + "\x00" + r["sl_key"]: r for r in
                    self.db.query("SELECT * FROM matches WHERE mapping_id=?", (mapping_id,))}
        # A match only needs recomputing when its row moved or the observations behind it did.
        versions = {side: (self.snapshot_versions(mapping_id, side),
                           self.snapshot_versions(mapping_id, side, previous=True)) for side in ("pl", "sl")}
        added = 0
        changed: list[int] = []
        for r in results:
            pair = r.pl_key + "\x00" + r.sl_key
            row = existing.get(pair)
            if row:
                moved = (row["pl_name"], row["pl_version"], row["sl_name"], row["sl_version"]) != (
                    r.pl_name, r.pl_version, r.sl_name, r.sl_version)
                if not moved:
                    for side, key in (("pl", r.pl_key), ("sl", r.sl_key)):
                        if key and versions[side][0].get(key) != versions[side][1].get(key):
                            moved = True
                            break
                if not moved:
                    continue
                self.db.execute(
                    "UPDATE matches SET pl_name=?, pl_version=?, sl_name=?, sl_version=?, updated_at=? WHERE id=?",
                    (r.pl_name, r.pl_version, r.sl_name, r.sl_version, now(), row["id"]),
                )
                changed.append(row["id"])
            else:
                cur = self.db.execute(
                    "INSERT INTO matches(mapping_id,pl_key,sl_key,method,confidence,status,rationale_json,mismatch_json,"
                    "pl_name,pl_version,sl_name,sl_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mapping_id, r.pl_key, r.sl_key, r.method, r.confidence, r.status,
                     json.dumps(r.rationale), json.dumps(r.mismatch_reasons), r.pl_name, r.pl_version,
                     r.sl_name, r.sl_version, now(), now()),
                )
                added += 1
                changed.append(cur.lastrowid)
        from .review import RecomputeContext, ReviewService, decisions_by_match  # deferred: module cycle
        if changed:
            review = ReviewService(self)
            rows = {r["id"]: r for r in self.db.query("SELECT * FROM matches WHERE mapping_id=?", (mapping_id,))}
            decisions = decisions_by_match(self.db, changed)
            for match_id in changed:
                review.recompute(match_id, context=RecomputeContext(
                    match=rows[match_id], decisions=decisions.get(match_id, []), inventory=sides))
        return {"status": "ok", "pl": len(pl), "sl": len(sl), "proposed": len(results), "new_matches": added}
```

Two deliberate deviations from the scout's sketch, both required:

1. **The staleness key is `source_version`, not `updated_at`.** `_store_snapshot` stamps `updated_at = now()` on
   every component row of every snapshot (`clreview/service.py:236`), so a refresh that changed nothing still moves
   `updated_at` on all N components — keying on it recomputes everything and buys nothing. `source_version`
   (`clreview/domain.py:100-108`) is a SHA-256 over exactly the inputs `recompute` reads: active copyrights,
   active licenses, license texts. Comparing the current and previous successful snapshots' `source_version` per
   component is a complete staleness test for the comparison, and it costs two extra queries.
2. **The match row's own columns are part of the test.** `recompute` writes `review_status` derived from
   `comparison()`, so a row whose stored `pl_name`/`pl_version`/`sl_name`/`sl_version` differ from what matching
   just produced must be updated and recomputed regardless of `source_version`.

Why the heuristic is safe — every writer of `recompute`'s inputs either recomputes itself or is caught above:

| input to `recompute` | who writes it | covered by |
|---|---|---|
| `matches.*` row | `_run_matching` | the row-comparison branch |
| `decisions` | `ReviewService.decide` (`review.py:360`) → `recompute` at `:370` | that call |
| `suggestions`, `notes` | `add_suggestion`, `add_note` | not read by `recompute` |
| component observations | `_store_snapshot` | `source_version` current-vs-previous |
| component name/version | `_store_snapshot` | match-row comparison + new match pair if the key moved |

First-ever matching (no previous successful snapshot) has nothing to compare against, so every match counts as
changed and gets recomputed — correct, and unavoidable.

---

## 4. Test — `tests/test_matching.py`

Append to the existing file (do not create a new module; `MatchingTests` stays byte-identical). Extend the import
block at the top to:

```python
from collections import Counter
from typing import Any

from clreview.db import DB
from clreview.domain import ComponentData, CopyrightEntry, LicenseEntry, LicenseTextCandidate, sha256
from clreview.service import IN_CHUNK, Service
```

Then add a module-level `inventory(n)` builder and two tests (full text verified in a scratch tree; paste as-is):

```python
STAMP = "2026-01-01T00:00:00+00:00"


def inventory(n: int) -> list[ComponentData]:
    """n components, identical on both sides: the shape that made matching superlinear."""
    out = []
    for i in range(n):
        name = "lib%d" % i
        out.append(ComponentData(
            external_id="%s|1.0" % name, name=name, version="1.0", purl="pkg:gen/%s@1.0" % name,
            copyrights=[CopyrightEntry(text="Copyright (C) 2020 Owner %d" % i, origin="kb")],
            licenses=[LicenseEntry(name="MIT", spdx="MIT")],
            license_texts=[LicenseTextCandidate(kind="source_file",
                                                text="Permission is hereby granted, free of charge, item %d" % i,
                                                provenance="src", content_hash=sha256("text%d" % i))],
        ))
    return out


class MatchingScaleTests(unittest.TestCase):
    """_run_matching must read in a bounded number of statements, not one per component."""

    N = 1600

    def setUp(self):
        self.db = DB(":memory:")
        self.svc = Service(self.db)
        for role, tool in (("pl", "mock-pl"), ("sl", "mock-sl")):
            self.db.execute("INSERT INTO connections(role,tool,name,created_at) VALUES(?,?,?,?)",
                            (role, tool, tool, STAMP))
        self.mid = self.db.execute(
            "INSERT INTO mappings(name,pl_connection_id,sl_connection_id,pl_project,pl_version,sl_scan,created_at)"
            " VALUES(?,?,?,?,?,?,?)", ("scale", 1, 2, "proj", "1.0", "scan", STAMP)).lastrowid
        self.store()

    def tearDown(self):
        self.db.close()

    def store(self) -> None:
        inv = inventory(self.N)
        self.svc._store_snapshot(self.mid, "pl", "mock-pl", inv)
        self.svc._store_snapshot(self.mid, "sl", "mock-sl", inv)

    def counted_matching(self) -> tuple[Counter, dict[str, Any]]:
        counts: Counter = Counter()
        original = {"query": self.db.query, "one": self.db.one, "execute": self.db.execute}

        def shape(sql: str) -> str:
            words = " ".join(sql.split()).split(" ")
            head = words[0].upper()
            if head == "SELECT":
                return "SELECT " + sql.split("FROM")[1].split()[0]
            if head == "UPDATE":
                return "UPDATE " + words[1]
            return head

        def wrap(fn):
            def inner(sql, params=()):
                counts["TOTAL"] += 1
                counts[shape(sql)] += 1
                if "component_id=?" in sql:
                    counts["PER_COMPONENT"] += 1
                return fn(sql, params)
            return inner

        for name, fn in original.items():
            setattr(self.db, name, wrap(fn))
        try:
            result = self.svc._run_matching(self.mid)
        finally:
            for name, fn in original.items():
                setattr(self.db, name, fn)
        return counts, result

    def test_matching_reads_in_bounded_statements(self):
        counts, result = self.counted_matching()
        self.assertEqual(result["proposed"], self.N)
        self.assertEqual(result["new_matches"], self.N)
        self.assertEqual(counts["PER_COMPONENT"], 0, "observations must be batched, never read per component")
        self.assertLess(counts["TOTAL"], 6000, dict(counts))
        ceiling = 2 * (self.N // IN_CHUNK + 1)
        for table in ("copyrights", "licenses", "license_texts"):
            self.assertLessEqual(counts["SELECT " + table], ceiling, dict(counts))

    def test_a_refresh_that_changed_nothing_recomputes_nothing(self):
        self.counted_matching()
        self.store()
        counts, result = self.counted_matching()
        self.assertEqual(result["new_matches"], 0)
        self.assertEqual(counts["INSERT"], 0, dict(counts))
        self.assertEqual(counts["UPDATE matches"], 0, dict(counts))
        self.assertLess(counts["TOTAL"], 200, dict(counts))
```

Both tests assert on **statement counts, never wall-clock** — deterministic in CI. Measured values: first run
`TOTAL 3244` (`SELECT copyrights/licenses/license_texts` 8 each, `PER_COMPONENT 0`); second run `TOTAL 41`,
0 inserts, 0 `UPDATE matches`. The pre-fix code fails all of it: `TOTAL 32008`, `PER_COMPONENT 19200`,
`UPDATE matches 1600`.

`unittest discover` collects both (plain `unittest.TestCase`, per the step-2 rule).

---

## 5. Files touched

| file | change |
|---|---|
| `clreview/service.py` | `IN_CHUNK` + `_chunks`; `components_observations`; `side_components_with_observations`, `_to_component_data`, `snapshot_versions`; `_inventory_for_matching` now 2 lines; `_run_matching` replaced |
| `clreview/review.py` | `dataclass` + `IN_CHUNK` imports; `decisions_by_match`; `RecomputeContext`; optional `inventory` on `_component`/`comparison`; optional `context` on `recompute` |
| `tests/test_matching.py` | import block + `inventory()` + `MatchingScaleTests` |

Untouched: `clreview/db.py` (no helper needed — `query`/`one`/`execute` already take a params tuple),
`clreview/matching.py` (the algorithm is fine; only its inputs were expensive), `clreview/api.py`,
`clreview/comparison.py`, `clreview/domain.py`, all other tests.

**No public behaviour changes.** `refresh()` returns the same dict shape; `GET /api/matches` and the review
endpoints see the same rows. One intentional difference: a no-op refresh no longer rewrites `matches.updated_at`
for every row, because nothing moved.

---

## 6. Verification

```bash
uv run pytest -q --ignore=adws                       # 117 passed, coverage gate (75) green — measured 77.02%
uv run pytest -q --no-cov tests/test_matching.py -v  # 9 passed (7 existing + 2 new scale tests)
uv run ruff check clreview tests                     # no issues
```

Baseline before starting: `115 passed`, coverage `76.61%`. Nothing may go red.

Independent semantics check (run in the pre-fix tree and the post-fix tree, diff must be empty) — the scenario
script is at `/tmp/scenario_b1.py` and covers: first refresh → one decision → refresh with changed copyright
text but identical names → refresh with a renamed component → no-op refresh, dumping
`pl_key, sl_key, method, status, review_state, review_status, discrepancy_count, pl_name, pl_version, sl_name,
sl_version` for every match plus `overview()["counts"]` after each step. Verified identical across both trees,
including the risky case (changed copyright text → `discrepancy_count` 2 → 3 in both).

Optional re-measure, if you want the numbers again: `/tmp/bench_b1.py` (copy into the repo root, run with
`PYTHONPATH=. uv run python /tmp/bench_b1.py`).

Commit as one commit: `Make refresh() matching scale: batched reads, incremental recompute (B1)`.

---

## 7. Out of scope (do not touch)

- B2-B9 (indexes and FK enforcement already landed in step 3; B8 store-snapshot batching is the natural next step —
  `_store_snapshot` is now the dominant cost of a refresh at 0.10 s of the 0.20 s total at N=1600).
- Every A/C/D/E/F finding.
- Any HTTP/API change, any concurrency or locking change (`clreview/db.py:141-166`).
- Caching beyond one call frame — no module-level caches, no `functools.lru_cache` on `current_snapshot`: it takes
  `mapping_id` and must not outlive a refresh, or a refresh reads a stale snapshot id.
- No new dependencies. No query rewrites in `comparison.py`/`matching.py`. No schema change.
