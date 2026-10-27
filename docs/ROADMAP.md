# Roadmap

## Delivered

| Phase | State |
|---|---|
| 1 Foundation: domain models, SQLite persistence, connector contract, config, runnable shell | done |
| 2 First integration: Black Duck + FOSSology retrieval, status/errors | done (connector code verified against reference repos; live credentials not available here) |
| 3 Matching and review: hierarchy, side-by-side copyright/license, text provenance | done |
| 4 Synchronization: preview, dry-run, field approvals, writes, verification, retries, audit | done |
| 5 Validation: fixtures, 102 automated tests, error handling, docs | done |
| 6 Extensibility: ScanCode connector, FOSSID placeholder, LLM suggestion model | done |
| 7 Capability audit: both vendor specs in `apis/`, Black Duck copyright + license-text writes | done |
| 8 Deploy: Black Duck BOM → id2url → FOSSology folder | done |

## Black Duck → FOSSology deploy (BOM to folder) — done

The working pattern this platform sits between: every library of a Black Duck project version is
uploaded to a FOSSology folder, cleared there, and the results are reviewed and pushed back. The
outbound half is `clreview/deploy.py`, reachable as `POST /api/deploy`; matching, review and sync
were not touched. Endpoint and resolver details are in [API-CAPABILITIES.md](API-CAPABILITIES.md).

1. Read the BOM — `BlackDuckConnector.source_coordinates()` collects `packageUrl` / origin id per
   component and makes no license-detail calls.
2. Resolve download URLs with [`id2url`](https://github.com/dineshr93/id2url): one TSV
   (`registry<TAB>coordinate`) per project version, one `id2url batch … --format json
   --continue-on-error` **subprocess** (id2url is AGPL-3.0 — process boundary, never an import).
   purl first, then the Black Duck origin namespace; an unknown registry is reported per component,
   never fatal. Fallbacks still open: SBOM `downloadLocation` → `originUrl` → operator VCS URL.
3. Create the target folder: default `{project}__{version}` from the display names (not the opaque
   Black Duck ids), sanitized (`/ \ : * ? " < > |` → `_`), overridable per request. `POST /folders`
   is idempotent but answers with a message, so the id is re-read from the folder listing.
4. `POST /uploads` per component into that folder: `uploadType=url` + `location.url`, upload name
   `{name}-{version}`, `scanOptions` for `nomos` + `copyright_email_author`, `mainLicense` when the
   caller passes one. A component whose upload name is already in the folder is `already_present`;
   two BOM components sharing one source upload once.
5. Report per component: `uploaded` / `already_present` / `no_source_url` / `unsupported_registry` /
   `upload_failed` / `would_upload` (dry run). A blanket ok is never reported. The run is audited as
   `deploy.fossology`.
6. The folder shows up as a scan (`folder:<id>`, name `… (folder)`) in the mapping picker, so the
   deployed version is reviewed as one mapping and synced back to Black Duck unchanged.

Still open, each needing a live instance to verify rather than a spec read:

- Poll `GET /jobs/{id}` / `GET /uploads/{id}/agents` before reading findings; `412`/`503` mean "not
  ready", not "no findings". Today the caller waits.
- sha1-based dedupe against `GET /uploads?folderId=&recursive=true`; dedupe is by upload name now.
- BOM write-back of `purl` / `downloadLocation`, and a BOM comment holding the FOSSology upload id.
- A UI affordance. The deploy runs over the API for now.

Request shape (`POST /api/deploy`):

```json
{
  "pl_connection_id": 1,
  "sl_connection_id": 2,
  "project_id": "…", "version_id": "…",
  "folder": "Q3-audit",              // optional; default "{project}__{version}"
  "parent_folder": 12,               // required unless the connection sets deploy_parent_folder
  "agents": ["nomos", "copyright_email_author"],
  "dry_run": true
}
```

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
