# Roadmap

## Delivered

| Phase | State |
|---|---|
| 1 Foundation: domain models, SQLite persistence, connector contract, config, runnable shell | done |
| 2 First integration: Black Duck + FOSSology retrieval, status/errors | done (connector code verified against reference repos; live credentials not available here) |
| 3 Matching and review: hierarchy, side-by-side copyright/license, text provenance | done |
| 4 Synchronization: preview, dry-run, field approvals, writes, verification, retries, audit | done |
| 5 Validation: fixtures, 63 automated tests, error handling, docs | done |
| 6 Extensibility: ScanCode connector, FOSSID placeholder, LLM suggestion model | done |

## FOSSID adapter

1. Verify the FOSSID API surface against a live instance: project/scan listing, component inventory,
   license and copyright findings, and any write endpoint for license declarations.
2. Implement read-only first (`read_components`, `read_copyrights`, `read_license_text`) and map its
   component identity into `ComponentData` (name, version, purl or package identifiers).
3. Declare write capabilities only after a documented endpoint is verified; otherwise keep them
   `False` so sync reports `unsupported_target`.
4. Register in `REGISTRY` with role `pl`; no review/sync changes are needed.

## ScanCode hardening

- Support `--json-pp` outputs that embed file content, and an option to point at a source tree for
  reading license files directly (with hash verification).
- Map ScanCode `license_detections` expressions (AND/OR/WITH) into declared/conjunctive/alternative
  license types instead of splitting tokens.

## LLM assistance (optional, human-gated)

1. Offline worker reads a component's evidence bundle (copyright rows, license rows, candidate
   license files with hashes) and posts `suggestions` with model id, confidence, and evidence.
2. UI shows suggestions separately; accept/reject is a human action recorded on the suggestion and
   turned into a normal decision.
3. Confidence thresholds only affect ordering/priority, never approval. No auto-sync path exists.
4. Evaluation: a labelled fixture set (matches, copyright duplicates, false positives, best license
   file) measures suggestion precision/recall before enabling a model in any deployment.
5. Audit keeps the model identity and the reviewer decision side by side.

## Operations

- Multi-user auth: per-user tokens and reviewer identity (current: shared `CLREVIEW_TOKEN` + `X-Reviewer`).
- Optional Postgres backend behind the same repository functions if concurrent reviewers grow.
- Background refresh jobs for large inventories, with per-side progress instead of a single request.
- Rate-limit aware scheduling for Black Duck/FOSSology APIs (currently bounded retries per request).
