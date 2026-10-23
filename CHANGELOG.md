# Changelog

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
