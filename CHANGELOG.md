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
- 102 stdlib tests (23 new deploy tests).

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
