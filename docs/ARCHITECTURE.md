# Architecture

## Layers

```
web/                     UI: overview, queue, workspace, history (vanilla JS + JSON API)
clreview/server.py       entry point: CLI flags, env fallbacks, demo seeding, graceful shutdown
clreview/api.py          HTTP routing, auth, error mapping, static files
clreview/review.py       queue, comparison presentation, decisions, notes, suggestions, statuses
clreview/sync.py         preview, dry-run, capability validation, writes, verification, retry
clreview/deploy.py       Black Duck BOM -> id2url -> FOSSology folder (POST /api/deploy)
clreview/service.py      connections, mappings, snapshot refresh, matching, overview, audit
clreview/connectors/     base contract + registry; blackduck, fossology, scancode, fossid, mock
clreview/matching.py     matching hierarchy and rationale
clreview/comparison.py   copyright/license/text comparison and provenance classification
clreview/domain.py       internal models, statuses, normalization
clreview/db.py           SQLite schema and query helpers
```

Vendor API objects never cross the connector boundary: connectors translate into
`ComponentData`, `CopyrightEntry`, `LicenseEntry`, `LicenseTextCandidate`.

## Data model

| Table | Holds |
|---|---|
| `connections` | role (`pl`/`sl`), tool, base URL, non-secret config, credential **env var names**, last test result |
| `mappings` | one PL connection + project/version and one SL connection + scan |
| `snapshots` | one retrieval per mapping/side: timestamp, status, error, item count, content hash |
| `components` | normalized identity per snapshot: name, version, purl, metadata, raw payload, `source_version`, `write_key` |
| `copyrights` | raw notice text, normalized form, hash, origin, source path, active, retrieved timestamp |
| `licenses` | name, SPDX, type (declared/conjunctive/alternative), evidence paths, active, source tool |
| `license_texts` | kind (`generic_metadata` / `source_file` / `unknown`), file name/path, text, content hash, provenance |
| `matches` | PL key ↔ SL key, method, confidence, status, rationale, mismatch reasons, review state, cached review status/discrepancy count |
| `decisions` | one row per (match, field, entry): action, approved value, reviewer, note, source versions at decision time |
| `suggestions` | machine-generated value, confidence, model, evidence, status (`open`/`accepted`/`rejected`), deciding reviewer |
| `notes` | reviewer notes per component |
| `sync_jobs` / `sync_items` | job mode/status/summary; item status, before value, approved value, API status/response, verification, attempts |
| `audit` | actor, action, entity, JSON detail, timestamp |

Raw observations, normalized comparison values, and approved values are separate columns/tables.
Nothing overwrites an observation.

Nine indexes back the reads the UI actually performs: `snapshots_lookup(mapping_id, side, status)`,
`components_key(snapshot_id, external_id)`, `matches_pair(mapping_id, pl_key, sl_key)`,
`matches_mapping(mapping_id)`, `decisions_key(match_id, field, entry_key)`, `notes_match(match_id)`,
`sync_items_job(job_id)`, `suggestions_match(match_id)` and `audit_entity(entity_type, entity_id)`.
`matches_mapping` is not redundant in practice: the planner uses it for `SELECT * FROM matches
WHERE mapping_id=?` and for the ordered listing, where the wider `matches_pair` prefix would need a
sort.

`PRAGMA foreign_keys=ON` is set on every connection, so the parent/child chains above are enforced
by SQLite rather than by convention: deleting a mapping cascades to its snapshots (and their
copyrights, licenses and texts), its matches (and their decisions, notes and suggestions) and its
sync jobs and items. A mapping's two connection references are `NOT NULL` without a cascade, so a
connection a mapping still uses is never silently dropped. `decisions` are unique
per `(match_id, field, entry_key)`, so re-deciding a field overwrites the row instead of stacking a
second approved value.

`source_version` is a hash of a component's active observations (copyright texts, license
names+SPDX+type, license text hashes+kinds). A decision stores the SL and PL `source_version`
seen at decision time; comparing them with the current snapshot yields `stale` (SL changed) and
`conflict` (PL changed) without mutating history.

## Refresh flow

1. Retrieve each side independently; a failed side stores an error snapshot but does not delete
   the previous successful snapshot.
2. Store a new snapshot + components + observations.
3. Re-run matching against the latest successful snapshots of both sides.
4. Keep existing match rows (confirmed/rejected/manual decisions survive; names refresh).
5. Recompute review status/discrepancy count for every match.

Review decisions are never deleted or silently overwritten by a refresh; staleness is surfaced in
the workspace and blocks synchronization.

## Matching

`clreview/matching.py` proposes matches in five passes: purl, name+version, strong identifier,
name-only (either a version absent on one side, or versions agreeing when the stronger rules did not
fire — both 0.72), and the fuzzy suggestion. Every result carries method, confidence, rationale and
mismatch reasons. Fuzzy results have status `suggested` and cannot be synchronized
until a reviewer confirms or reassigns them.

## Review decisions

`decide(match, field, entry_key, action, approved_value, note, reviewer)` validates the action
against the actual comparison rows:

- `approve_sl` requires an SL entry (copyright text hash, license name key, or source-file text hash).
- `accept_pl` requires a PL entry.
- `correct` requires a value; for license text the value must equal an identified SL source-file
  candidate (no invented text).
- `reject` / `mark_false_positive` / `mark_irrelevant` retain the rejection and remove the
  discrepancy from the pending set; it is not presented as a new discovery after refresh.
- `note_only` records a note without resolving the field.

Review status: `not_reviewed` (no discrepancies), `review_required` (discrepancies, no decisions),
`in_progress` (some fields decided), `reviewed` (all discrepant fields decided), then an explicit
`approved_for_sync` transition (requires no undecided discrepancies and at least one approved
SL-derived correction).

## Synchronization flow

`SyncService.create_job` collects decisions with `approve_sl`/`correct` from components whose
review state is `approved_for_sync`, builds preview items, validates capabilities, and stores a
job. Dry-run stops there. Live execution, per item:

1. reject unsupported fields (`unsupported_target`), stale SL versions (`stale`), changed PL
   versions (`conflict`), and values already present (`skipped_already_applied`);
2. read current PL values;
3. call the connector write;
4. verify by re-reading PL when the connector declares `verify_writes`;
5. record API status/response, verification value, message, attempts;
6. audit each item and finish the job with a summary.

Retry creates a new job containing only failed items. Because values already present are skipped,
repeated synchronization does not duplicate entries.

## Extensibility

`clreview/connectors/base.py` holds the protocol and a registry keyed by tool name with supported
roles and a factory. Adding a tool means adding one module plus one registry entry; the review and
sync workflows are unchanged. Roles are configuration, not vendor identity — a tool declares which
roles it can play.

## LLM readiness

`suggestions` stores machine output separately from `decisions`. A suggestion carries its value,
confidence, model identity, evidence, and final reviewer decision. Accepting a suggestion goes
through the same validation as a human decision and creates a normal `decisions` row; rejecting it
records the rejection. Synchronization only reads decisions, so no machine can approve or write
without a human action.
