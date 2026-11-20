# Architecture

## Layers

```
web/                    UI: overview, queue, workspace, history, settings (vanilla JS + JSON API)
clreview/server.py      entry point: CLI flags, env fallbacks, demo seeding, graceful shutdown
clreview/api.py         HTTP routing, auth, error mapping, static and export bodies
clreview/review.py      queue, comparison, decisions, notes, suggestions, false positives, delta, export
clreview/sync.py        preview, dry-run, capability validation, writes, verification, retry, per-component apply
clreview/undo.py        restore a live job from stored before-values
clreview/deploy.py      Black Duck BOM -> id2url -> FOSSology folder (POST /api/deploy)
clreview/service.py     connections, mappings, snapshots, matching, overview, audit, license map
clreview/connectors/    base contract + registry; blackduck, fossology, scancode, fossid, mock
clreview/matching.py    matching hierarchy and rationale
clreview/comparison.py  copyright/license/text comparison and provenance classification
clreview/domain.py      internal models, statuses, normalization
clreview/db.py          SQLite schema, migrations, query helpers
clreview/config.py      minimal profiles loader for config.yaml
clreview/folder_mapping.py folder-name template/sanitizer used by deploy
```

Vendor API objects never cross the connector boundary: connectors translate into
`ComponentData`, `CopyrightEntry`, `LicenseEntry`, `LicenseTextCandidate`.
Capability flags are authored by the connector, not inferred from a vendor name.

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
| `license_map` | FO short name → SPDX → BD license identifier; seeded, editable, auditable |
| `false_positive_rules` | license name, evidence-path pattern, scope (`component`/`version`/`global`), match/mapping ids, revocation timestamp |
| `audit` | actor, action, entity, JSON detail, timestamp |

Raw observations, normalized comparison values, and approved values are separate columns/tables.
Nothing overwrites an observation. `sync_items.pl_before` stores the target value seen before a
live write, so undo can replay it rather than guessing from a new read.

Indexes back the reads the UI performs: snapshots lookup, component identity, match pair, mapping
ordering, decision keys, notes, sync items, suggestions, audit entity, and
`false_positive_scope(scope, mapping_id, match_id, revoked_at)`.

`PRAGMA foreign_keys=ON` is set on every connection. Deleting a mapping cascades to its snapshots,
observations, matches, decisions, notes, suggestions and sync jobs/items; connection references are
`NOT NULL` without cascade. Decisions are unique per `(match_id, field, entry_key)`.

`source_version` is a hash of a component's active observations. Decisions store the SL and PL
source versions seen at decision time; comparing them with the current snapshot yields `stale`
(SL changed) and `conflict` (PL changed) without mutating history.

## Refresh flow

1. Retrieve each side independently; a failed side stores an error snapshot but keeps the previous
   successful snapshot.
2. Store a new snapshot + components + observations.
3. Load the license map and active false-positive rules once, then re-run matching against the latest
   successful snapshots of both sides.
4. Keep existing match rows (confirmed/rejected/manual decisions survive; names refresh).
5. Recompute review status/discrepancy count for every match.

The cached license map and rule list keep bulk recomputation from issuing one query per match.
Review decisions are never deleted or silently overwritten by a refresh; staleness is surfaced in the
workspace and blocks synchronization.

## Matching

`clreview/matching.py` proposes matches in six passes over one name rule (`domain.name_key`:
letter runs + version): purl, name text + version, strong identifier, name-only (version absent on
one side, or agreeing — 0.72), then two suggestions: `name_subset` (SL text a strict subset of PL's
at the same version) and `version_differs` (same text, closest version). Every result carries method,
confidence, rationale and mismatch reasons. Suggestions have status `suggested` and cannot be
synchronized until a reviewer confirms or reassigns them.

`matching.SLScope` mirrors those passes on identity alone. `Service.refresh` reads SL first, builds
the scope, and passes it to the PL read: Black Duck (`supports_scope`) filters its BOM listing before
the per-component origin and copyright requests; any other PL tool is filtered after reading. What
the scope leaves out is recorded in `pl_listing` (key, name, version) beside the PL snapshot, for
the manual-match picker only. License comparison uses the editable license map to translate an SL
short name/SPDX value into the expected BD license identifier.

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

### False positives and license mapping

A `mark_false_positive` decision can store `scope` (`component`, `version` or `global`)
and an evidence-path `path_pattern`. The rule is applied automatically only to `sl_only` license
rows whose normalized license name matches and whose evidence path matches the pattern. Revoking a rule
(`POST /api/false-positive-rules/{id}/revoke`) makes the finding surface again; rules are visible in
settings. Component approval writes the mapped BD license identifier (from `license_map`), while
license text still comes from SL source evidence.

`license_catalog.propose` builds map proposals from three sources. The SL side is the license names
in SL snapshots plus FOSSology's `/license/{shortName}` for each, read per name rather than through the
catalogue, which carries every text. The PL side is the Black Duck `/api/licenses` catalogue plus the
BOM names. The third source is the SPDX list (`CL_SPDX_URL`), read once per process. All three are
matched with `domain.license_key`: the component name rule, except that numbers must be equal instead
of being compared as a version. A row with source `manual` is never proposed over. Applying writes
source `suggested`, in one transaction, and updates an existing row whatever the case of its name.

### Delta mode

`GET /api/mappings/{id}/delta` finds the previous mapping for the same PL project and connection
(older `created_at`, different PL version). It compares the current and previous PL snapshots and
also checks matched SL source versions, reporting new components, changed components, and newly added
licenses/copyrights. `POST /api/mappings/{id}/delta/apply` carries prior decisions into the new
matches by `pl_key`, skipping duplicates, then recomputes. Decisions are carried, not silently
re-approved.

## Synchronization flow

`SyncService.create_job` collects decisions with `approve_sl`/`correct` from components whose
review state is `approved_for_sync`, builds preview items, validates capabilities, and stores a job.
Dry-run stops there. Live execution, per item:

1. reject unsupported fields (`unsupported_target`), stale SL versions (`stale`), changed PL
   versions (`conflict`), and values already present (`skipped_already_applied`);
2. read current PL values;
3. call the connector write;
4. verify by re-reading PL when the connector declares `verify_writes`;
5. record API status/response, verification value, message, attempts;
6. if the job was created with `finalize=true` and every field write for the component succeeded,
   PUT `reviewStatus=REVIEWED` on the BOM component and PATCH the matching FOSSology upload to
   `Closed` with the comment `reviewed`; otherwise record the final items as
   `skipped_prerequisite`;
7. audit each item and finish the job with a summary.

`POST /api/components/{id}/apply` is the single-component path: it previews/finalizes one approved
match, writes its approved fields, then marks the local match `reviewed` only when both the BD
field writes and the final fields succeed. FO close happens only after the BD writes; failures leave
the final items skipped.

Retry creates a new job containing failed and interrupted items plus final items skipped because a
field failed. Already-present values make repeated synchronization converge instead of duplicate.

A live job runs inside the request that created it, so a process that dies between items leaves the
job `in_progress`. At startup `reconcile_interrupted_jobs()` settles it and audits recovery:
unwritten items become retryable, while a job whose writes all landed is classified as completed.

## Undo

`GET /api/sync/jobs/{id}/undo/preview` reads stored `pl_before` values and reports whether each
field is restorable. `POST /api/sync/jobs/{id}/undo` requires `confirm=true` and restores
license names, copyrights, license text, BD review status and FO upload status where capabilities
allow. Dry-run jobs have nothing to undo; unsupported connector capabilities or missing before-values
are reported as manual actions. The job is marked `undone` and audited.

## Exports and audit

`GET /api/mappings/{id}/export.csv` and `export.html` render the review queue as raw
`text/csv` and `text/html` bodies with attachment filenames. Export does not require a reviewer.
Audit rows record decisions, notes, sync items, deploy, undo, license-map changes, rule creation and
revocation; tokens are never logged or returned.

## Safety and read-only mode

`read_only` refuses vendor writes: sync jobs, retries, deploy, per-component apply and undo answer
409. Local actions — decisions, notes, mappings, refresh, license-map edits, export —
keep working. The guard is enforced by the route handler set, not by the connector.

## Schema versioning

`clreview/db.py` stamps `PRAGMA user_version` with `SCHEMA_VERSION` on every open. The current
schema is version 2. `CREATE ... IF NOT EXISTS` cannot add a column to an existing database, so a
schema change means bumping the version and adding the matching migration step. A database stamped
with a newer version than the build refuses to open instead of being written into a half-known shape.

## Extensibility

`clreview/connectors/base.py` holds the protocol and a registry keyed by tool name with supported
roles and a factory. Adding a tool means adding one module plus one registry entry; the review and sync
workflows are unchanged. Roles are configuration, not vendor identity — a tool declares which roles it
can play.

## LLM readiness

`suggestions` stores machine output separately from `decisions`. A suggestion carries its value,
confidence, model identity, evidence, and final reviewer decision. Accepting a suggestion goes through
the same validation as a human decision and creates a normal `decisions` row; rejecting it records
the rejection. Synchronization only reads decisions, so no machine can approve or write without a human
action.
