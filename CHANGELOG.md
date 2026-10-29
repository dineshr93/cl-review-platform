# Changelog

## Unreleased

- Audited both vendor API specs into the repo (`apis/blackduck_Blackduck-REST-API-v2026.4.2.md`,
  `apis/fossology_openapi.yaml`) and captured the result as `docs/API-CAPABILITIES.md`.
- Black Duck connector now writes copyright statements
  (`POST …/origins/{oid}/copyrights`, additive) and component license text
  (`PUT …/components/{c}/licenses/{l}/text`); `write_copyrights` and `write_license_text` are no
  longer reported unsupported. `read_component` returns copyrights and BOM license text so every
  write is verified by re-read.
- Docs corrected: the two Black Duck "cannot be written" limitations were wrong; FOSSology's
  copyright API edits findings but cannot create them.
- Specified the Black Duck → FOSSology deploy step: `id2url` resolves every registry origin id to a
  source download URL (subprocess boundary, AGPL-3.0), uploads into a `{project}__{version}` or
  custom-named FOSSology folder (`docs/API-CAPABILITIES.md`, `docs/ROADMAP.md`).
- Implemented that deploy step: `clreview/deploy.py` behind `POST /api/deploy`. It reads the BOM
  (`source_coordinates`, identity only), maps purl / origin id to an id2url registry and coordinate,
  runs one `id2url batch` subprocess per project version, creates or reuses the FOSSology folder
  (`ensure_folder`), and uploads each source archive by URL (`upload_url`) with `nomos` +
  `copyright_email_author`. Dry run by default; per-component report (`uploaded`, `already_present`,
  `no_source_url`, `unsupported_registry`, `upload_failed`, `would_upload`) and one audit event.
  FOSSology folders are now selectable as scans (`folder:<id>`), so a deployed version is reviewed
  as one mapping and synced back unchanged.
- Component identity parsed from names keeps registry qualifiers (`guava-31.1-jre` → version
  `31.1-jre`, not `31.1`), which is what makes a deployed folder match its Black Duck BOM.
- **Source deployment** panel in the web UI (`#/deploy`): pick tool, project, version, parent folder
  and folder name; preview resolves every source without writing; upload stays gated behind a
  matching preview plus an explicit approval tick, and any change to the target invalidates it. A
  live run offers to create the mapping for the deployed folder. Backed by `GET /api/tools` now
  advertising which tools can deploy and `GET /api/connections/{id}/folders` for parent picking.
- Correctness: a Black Duck copyright write is one `POST` per notice the tool does not already
  have, verified by re-read; FOSSology findings are grouped before their grouping key is reused;
  a connection `config` may not carry a credential (the API answers 400 and names the key); a
  malformed numeric id in a path or query is a 400, not a traceback.
- CI is honest: one runner instead of a matrix that could not fail, dev dependencies installed from
  `uv.lock`, a coverage floor, `ruff` as a job, and releases built from the tag and attested with
  build provenance.
- SQLite: indexes for the reads the UI actually performs, and `PRAGMA foreign_keys=ON`, so deleting
  a match cascades to its decisions, notes and suggestions instead of orphaning them.
- `refresh()` scales: components and their observations are read in batches rather than per
  component, and matching recomputes only the sides whose snapshot changed (compared by
  `source_version`), leaving decisions on untouched components alone.
- Connector correctness: no silent retry of a `POST`/`PATCH` that may already have landed, a page
  cap that raises instead of paginating forever, license expressions split on whitespace-delimited
  `AND`/`OR` (so `GPL-3.0-or-later` survives), a bodyless POST answered 200 and a chunked one
  answered 411, and multi-write operations committed as one transaction.
- The mock connectors' documented fixture default is now real: `config.fixtures` omitted resolved to
  `<repo>/fixtures/demo` (one level too high, so a bare `MockPLConnector()` raised
  `FileNotFoundError`) instead of the packaged `clreview/fixtures/demo`.
- Read scale: one read context per sync preview, page-scoped queue reads, a breadth-first FOSSology
  tree walk that opens only the directories that can hold a wanted path, a per-call Black Duck
  response memo, snapshot retention of five per side, and copyright writes batched per component.
- Maintainability: dead code deleted, the three "this tool cannot write" refusals are one mixin, the
  HTTP surface is a single route table, the oversized handlers and browser renderers are split, and
  every scanner-derived string reaching the page is escaped.
- Tests: 276 stdlib tests (63 at 1.0.0). Every message the code raises is asserted somewhere, the
  failure, empty and race paths are covered (a refresh that fails mid-flight, empty inventories, an
  overwritten decision, two threads deciding the same field), and the suite leaves no temp
  directories or open database handles behind.
- Tooling: `.dockerignore` no longer ships session data or the operator's `.env` into the image, CI
  builds the image and fails when `/api/health` does not answer, every action is pinned to a commit
  SHA, packages are built with `uv`, the base image is pinned by digest, and the container
  healthcheck follows `CLREVIEW_PORT` instead of assuming 8765.
- 276 stdlib tests; the browser smoke test now drives the whole deploy panel —
  gate, preview, invalidation, live run and the mapping it creates.

## 1.0.0

- Copyright and license review workspace with side-by-side PL/SL comparison and field-level decisions.
- Matching hierarchy (purl, name+version, strong id, name-only, fuzzy suggestion requiring confirmation).
- License-text provenance: generic metadata versus source-file text; only identified license-file
  text can be synchronized.
- Controlled SL→PL synchronization: preview, dry run, capability validation, verification, retry,
  idempotent re-runs, per-component report, audit trail.
- Connectors: Black Duck, FOSSology, ScanCode, FOSSID placeholder, mock PL/SL; configurable roles.
- SQLite persistence, JSON API, static SPA, optional bearer auth, environment-based credentials.
- Machine suggestions stored separately from human decisions (LLM-ready, no model required).
- 63 stdlib tests including an HTTP end-to-end workflow.
