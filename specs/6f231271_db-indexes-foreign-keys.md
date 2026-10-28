# Plan — attack step 3: B2 (indexes) and B3 (foreign keys)

Repo: `/home/dinesh/repos/foss_data_reviewer`, on top of `4c60567` (steps 1 and 2 landed: CI now runs `uv run pytest -q --ignore=adws` with a `--cov-fail-under=75` gate and `ruff check`). Order: **B2 then B3**, one commit each. Two files: `clreview/db.py` and a new `tests/test_db_schema.py`.

Everything below was measured on this checkout, including a full dry run of the exact patch in a scratch copy.

## Baseline (measured)

`PRAGMA foreign_key_check` on **this repo's `clreview.db`** — the number the finding asked for:

```
clreview.db  foreign_keys pragma = 0   indexes = components_key, matches_pair, decisions_key
             rows: audit 292, components 282, copyrights 336, decisions 12, license_texts 249,
                   licenses 289, mappings 34, matches 7, notes 58, snapshots 80, suggestions 0,
                   sync_items 56, sync_jobs 56, connections 2
             PRAGMA foreign_key_check -> 0 rows
             13 hand-written LEFT JOIN orphan probes (every REFERENCES column, incl.
             mappings.pl_connection_id / sl_connection_id) -> all 0
freshly seeded test DB (DB(path) + seed_demo) -> PRAGMA foreign_key_check -> 0 rows
```
**No pre-existing violation anywhere**, so `PRAGMA foreign_keys=ON` can be turned on without a data migration. Nothing to handle deliberately — say so in the commit message and move on.

Query plans on `clreview.db` before the change (`EXPLAIN QUERY PLAN`):

| request path | before |
|---|---|
| `matches WHERE mapping_id=?` (`service.py:276,297,332`) | `SEARCH … USING INDEX matches_pair (mapping_id=?)` — already a seek |
| `snapshots WHERE mapping_id=? AND side=? AND status='ok' ORDER BY id DESC LIMIT 1` (`service.py:170`) | `SCAN snapshots` |
| `snapshots WHERE mapping_id=? AND side=? ORDER BY id DESC LIMIT 1` (`service.py:308`) | `SCAN snapshots` |
| `sync_items WHERE job_id=?` (`sync.py:154,265,278`) | `SCAN sync_items` |
| `notes WHERE match_id=?` (`review.py:199`) | `SCAN notes` |
| `suggestions WHERE match_id=?` (`review.py:391`) | `SCAN suggestions` |
| the audit OR-query (`service.py:355-356`) | `SCAN audit` + `LIST SUBQUERY` |

Two facts worth recording before touching anything:

- **`matches(mapping_id)` is redundant.** `matches_pair` is `(mapping_id, pl_key, sl_key)`, so `mapping_id` is already its leftmost prefix and the planner uses it. It is added anyway because this step's exit gate counts six indexes; after adding it the planner just switches to the new one (measured), so there is no read regression — but it is a pure write cost on a table every refresh rewrites. **Leave a one-line comment in the schema saying so**, and flag it for deletion in the B4/B5 step.
- **There is no `DELETE FROM` anywhere in `clreview/` or `tests/`.** So B3 changes no existing behaviour: the cascade it enables is future-proofing plus the regression test that pins it. No app path relies on today's orphan-leaving behaviour.

Dry run of the exact patch below (scratch copy): full suite green — `114 passed` (browser smoke deselected: the scratch copy has no seeded `clreview.db`), `Required test coverage of 75% reached. Total coverage: 76.61%`; deleting a mapping zeroed all 11 cascaded tables; a dangling child insert raised `sqlite3.IntegrityError: FOREIGN KEY constraint failed`; a DB file with two indexes dropped regained all nine on reopen; every plan above went `SCAN` → `SEARCH`.

---

## B2 — the six indexes

**File:** `clreview/db.py`, `SCHEMA`. Follow the file's existing convention — the index sits immediately after the table it serves (as `components_key`, `matches_pair`, `decisions_key` do). Six additions, each with its anchor:

| insert | anchor (unique in the file) |
|---|---|
| `CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots(mapping_id, side, status);` | before `CREATE TABLE IF NOT EXISTS components(` |
| `CREATE INDEX IF NOT EXISTS matches_mapping ON matches(mapping_id);` | after `CREATE UNIQUE INDEX IF NOT EXISTS matches_pair ON matches(mapping_id, pl_key, sl_key);` |
| `CREATE INDEX IF NOT EXISTS notes_match ON notes(match_id);` | before `CREATE TABLE IF NOT EXISTS sync_jobs(` |
| `CREATE INDEX IF NOT EXISTS sync_items_job ON sync_items(job_id);` | before `CREATE TABLE IF NOT EXISTS suggestions(` |
| `CREATE INDEX IF NOT EXISTS suggestions_match ON suggestions(match_id);` | before `CREATE TABLE IF NOT EXISTS audit(` |
| `CREATE INDEX IF NOT EXISTS audit_entity ON audit(entity_type, entity_id);` | after `  detail_json TEXT NOT NULL DEFAULT '{}'` + `);` (last statement in `SCHEMA`) |

With the redundancy note on the one redundant line, e.g.:

```sql
CREATE INDEX IF NOT EXISTS matches_mapping ON matches(mapping_id);  -- redundant with matches_pair's prefix; kept for now, drop with B4/B5
```

`IF NOT EXISTS` is what makes this an in-place upgrade: `DB.__init__` runs `executescript(SCHEMA)` on every open, so a database written by an older release gains the indexes on next start (verified: dropped two, reopened, all nine present). No migration, no version stamping.

`snapshots_lookup(mapping_id, side, status)` also serves `service.py:308`, which filters only `mapping_id` + `side`: measured plan becomes `SEARCH snapshots USING INDEX snapshots_lookup (mapping_id=? AND side=?)` plus a temp b-tree for `ORDER BY id DESC` over that (tiny) row set. Do not add a fourth column to chase the sort.

Expected plans after the change (all measured):

```
matches by mapping   SEARCH matches USING INDEX matches_mapping (mapping_id=?)
current snapshot     SEARCH snapshots USING INDEX snapshots_lookup (mapping_id=? AND side=? AND status=?)
sync_items by job    SEARCH sync_items USING INDEX sync_items_job (job_id=?)
notes by match       SEARCH notes USING INDEX notes_match (match_id=?)
suggestions by match SEARCH suggestions USING INDEX suggestions_match (match_id=?)
audit by entity      SEARCH audit USING INDEX audit_entity (entity_type=? AND entity_id=?)
audit OR-query       MULTI-INDEX OR -> SEARCH audit USING INDEX audit_entity on both branches
```

---

## B3 — turn on foreign key enforcement

**File:** `clreview/db.py`, `DB.__init__` (currently lines ~136-141). The pragma is **per connection**, and `DB.__init__` is the only place a connection is opened — `sqlite3.connect` appears nowhere else in `clreview/` or `tests/` (verified by grep), and `server.py:49` is the only production construction site.

```python
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # The schema's ON DELETE CASCADE clauses are inert unless every connection asks for them.
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
```
Order matters: the pragma must run **before** `executescript` (which commits any open transaction) and while no transaction is open — at this point in `__init__` none is. Setting it after `executescript` also works, but before keeps "the connection is configured, then used" obvious. Do not touch the lock at `db.py:141-166` (out of scope) and do not add `PRAGMA foreign_key_check` at startup — that is a separate finding.

Consequences to accept, both verified:

- `DELETE FROM mappings WHERE id=?` now cascades to `snapshots → components → copyrights/licenses/license_texts`, `matches → decisions/notes/suggestions`, and `sync_jobs → sync_items`. `audit` has no FK by design and survives — that is the reviewer's trail.
- A child row pointing at a missing parent now raises `sqlite3.IntegrityError`. No current code path can produce one (all FK columns are filled from a row that was just read or inserted), and the suite is green. If it ever fires through the API it lands in the catch-all `except Exception` → 500, which is the right answer for a broken invariant.
- Still unenforced, and deliberately left alone (schema redesign is out of scope): `decisions.mapping_id`, `notes.mapping_id`, `suggestions.mapping_id` and `sync_items.match_id` carry no `REFERENCES`. Mention them in the commit message as a known gap, do not add columns or constraints.

---

## Tests — new file `tests/test_db_schema.py`

Schema/index/cascade guarantees do not belong in `tests/test_db_concurrency.py` (that file is about the lock) and there is no `tests/test_db.py`. New file, `unittest.TestCase` classes so **both** runners collect it (the C1 lesson: a module-level `def test_…` is invisible to `unittest discover`). Paste-ready and verified green:

```python
"""Schema guarantees: the indexes the request paths need and the FK cascade the schema promises."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from clreview.db import DB
from clreview.server import seed_demo

NEW_INDEXES = ("snapshots_lookup", "matches_mapping", "notes_match", "sync_items_job",
               "suggestions_match", "audit_entity")
CASCADED = ("snapshots", "components", "copyrights", "licenses", "license_texts", "matches",
            "decisions", "notes", "suggestions", "sync_jobs", "sync_items")


def index_names(db: DB) -> set[str]:
    rows = db.query("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
    return {r["name"] for r in rows}


class SchemaIndexTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def tearDown(self):
        self.db.close()

    def test_every_hot_path_index_exists(self):
        missing = set(NEW_INDEXES) - index_names(self.db)
        self.assertFalse(missing, f"missing indexes: {sorted(missing)}")

    def test_planner_uses_the_new_indexes(self):
        for label, sql in (
            ("snapshots_lookup", "SELECT * FROM snapshots WHERE mapping_id=1 AND side='pl' AND status='ok'"),
            ("sync_items_job", "SELECT * FROM sync_items WHERE job_id=1"),
            ("notes_match", "SELECT * FROM notes WHERE match_id=1"),
            ("suggestions_match", "SELECT * FROM suggestions WHERE match_id=1"),
            ("audit_entity", "SELECT * FROM audit WHERE entity_type='match' AND entity_id='1'"),
        ):
            plan = " ".join(r["detail"] for r in self.db.query("EXPLAIN QUERY PLAN " + sql))
            self.assertIn(label, plan, f"{label} not used: {plan}")

    def test_database_created_before_the_change_upgrades_on_next_open(self):
        path = os.path.join(tempfile.mkdtemp(prefix="clreview-test-"), "legacy.db")
        self.addCleanup(shutil.rmtree, path.rsplit(os.sep, 1)[0], ignore_errors=True)
        DB(path).close()
        con = sqlite3.connect(path)
        con.executescript("DROP INDEX IF EXISTS notes_match; DROP INDEX IF EXISTS audit_entity;")
        con.close()
        self.db.close()
        self.db = DB(path)  # a database file written by an older release
        self.assertFalse(set(NEW_INDEXES) - index_names(self.db))


class ForeignKeyTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.mid = seed_demo(self.db)["mapping_id"]
        match = self.db.one("SELECT id FROM matches LIMIT 1")["id"]
        self.db.execute("INSERT INTO decisions(mapping_id,match_id,field,action,created_at) VALUES(?,?,'copyright','approve_sl','now')",
                        (self.mid, match))
        self.db.execute("INSERT INTO notes(mapping_id,match_id,body,created_at) VALUES(?,?,'note','now')", (self.mid, match))
        self.db.execute("INSERT INTO suggestions(mapping_id,match_id,field,value,created_at) VALUES(?,?,'copyright','x','now')",
                        (self.mid, match))
        self.db.execute("INSERT INTO sync_jobs(mapping_id,dry_run,status,created_at) VALUES(?,0,'completed','now')", (self.mid,))
        job = self.db.one("SELECT id FROM sync_jobs ORDER BY id DESC LIMIT 1")["id"]
        self.db.execute("INSERT INTO sync_items(job_id,match_id,field,status,updated_at) VALUES(?,?,'copyright','succeeded','now')",
                        (job, match))

    def tearDown(self):
        self.db.close()

    def test_foreign_keys_are_on_for_every_connection(self):
        self.assertEqual(self.db.query("PRAGMA foreign_keys")[0]["foreign_keys"], 1)

    def test_fresh_database_has_no_foreign_key_violations(self):
        self.assertEqual(self.db.query("PRAGMA foreign_key_check"), [])

    def test_dangling_child_row_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO snapshots(mapping_id,side,tool,retrieved_at) VALUES(999999,'pl','x','now')")

    def test_deleting_a_mapping_leaves_no_orphan_rows(self):
        self.assertTrue(self.db.query("SELECT id FROM snapshots"))
        self.db.execute("DELETE FROM mappings WHERE id=?", (self.mid,))
        for table in CASCADED:
            self.assertEqual(self.db.query(f"SELECT id FROM {table}"), [], f"{table} kept orphan rows")
        # the audit trail is deliberately not a child of a mapping: it outlives it
        self.assertTrue(self.db.query("SELECT id FROM audit"))


if __name__ == "__main__":
    unittest.main()
```
Seven tests, all passing in the dry run. `tests/test_review.py` and `tests/test_sync.py` need **no** edits — they are regression coverage that the pragma does not break a real refresh/decide/sync flow; if either goes red, the pragma was placed wrong (inside a transaction) or an insert really does dangle.

---

## Files touched

| file | change |
|---|---|
| `clreview/db.py` | B2: six `CREATE INDEX IF NOT EXISTS` lines in `SCHEMA`; B3: one `PRAGMA foreign_keys=ON` in `DB.__init__` |
| `tests/test_db_schema.py` | new: `SchemaIndexTests` (3) + `ForeignKeyTests` (4) |

No docs to update: nothing in `README.md`, `DESIGN.md` or `docs/` claims anything about indexes or foreign keys (grep), and `SECURITY.md` is untouched by this step.

## Verification

```bash
uv run pytest -q --ignore=adws
#   expect: "Required test coverage of 75% reached. Total coverage: 76.6%", "115 passed" locally (the browser smoke skips in CI, where it reads "114 passed, 1 skipped"), exit 0
uv run pytest -q --no-cov tests/test_db_schema.py -v        # 7 passed (single-file runs need --no-cov under the gate)
uv run ruff check clreview tests                            # exit 0
uv run python -c "
from clreview.db import DB
d = DB(':memory:')
print('foreign_keys =', d.query('PRAGMA foreign_keys')[0]['foreign_keys'])
print('indexes =', sorted(r['name'] for r in d.query(\"SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'\")))
print('fk_check =', d.query('PRAGMA foreign_key_check'))
"
#   expect: foreign_keys = 1, nine indexes (3 old + 6 new), fk_check = []
uv run python -c "
import sqlite3; c = sqlite3.connect('clreview.db')
print('repo clreview.db fk_check rows:', len(c.execute('PRAGMA foreign_key_check').fetchall()))
"
#   expect: 0  (re-check after the change; it was 0 before)
git diff --stat        # two files, no scope creep
```
The browser smoke (`tests/test_web_smoke.py`) passes locally only because `clreview.db` exists in the repo root and skips in CI (no chromium) — unchanged from step 2; do not chase it.

## Out of scope

B1 and B4–B9 (batch loading, N+1 rewrites, snapshot pruning, memoization), every A/C/D/E/F finding, any query rewrite, ORM or schema redesign, data migrations (none are forced — `foreign_key_check` is clean), changes to the locking in `clreview/db.py:141-166`, and adding FKs to the four unenforced columns listed above. Removing the redundant `matches_mapping` index is a later step's call, not this one's.
