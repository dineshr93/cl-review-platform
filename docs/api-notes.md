# API notes

This file holds the Phase 0 vendor research and the local HTTP contract. The vendor claims are
checked against the specifications kept in `apis/`; the local routes are the ones actually shipped
in `clreview/api.py`.

## Sources and what is verified

| Source | What it establishes |
|---|---|
| `apis/blackduck_Blackduck-REST-API-v2026.4.2.md` | Black Duck REST surface, media types, read/write verbs |
| `apis/blackduck_all-properties.md` / `apis/blackduck_using-environment-variables.md` | BOM fields and client environment conventions |
| `apis/fossology_openapi.yaml` | FOSSology REST surface and write payloads |
| `blackducksoftware/hub-rest-api-python`, `dineshr93/easy_fossy` | reference-client checks, not executed against a live server |

The live Black Duck and FOSSology instances were **not reachable** when this was written. Endpoint
behaviour below is spec/reference-client verified. Anything instance-dependent remains in
[API-CAPABILITIES.md](API-CAPABILITIES.md) and [LIMITATIONS.md](LIMITATIONS.md).

## Local API contract

Base URL: `http://127.0.0.1:8765` (loopback by default).

- Requests that send a body use `Content-Type: application/json` and a bounded
  `Content-Length`; bodies above 1,000,000 bytes are rejected. Chunked bodies are rejected with
  `411` because there is no length to bound the read against.
- JSON responses are `application/json`, with `Content-Length`, `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and `Cache-Control:
  no-store`.
- Authenticated deployments set `CLREVIEW_TOKEN` and send `Authorization: Bearer <token>`.
  `GET /api/health` stays public for orchestrators.
- `X-Reviewer: <name>` is the reviewer identity. It is required for every POST/DELETE except
  `POST /api/sync/preview`, which only computes; an unattributed decision is refused rather than
  recorded under a placeholder.
- `DELETE /api/license-map/{id}` is dispatched by the same handler as GET/POST; the route is
  documented and tested.
- The export routes return raw bodies, not JSON:
  - `GET /api/mappings/{id}/export.csv` → `text/csv; charset=utf-8`, attachment filename
    `review-<mapping>.csv`.
  - `GET /api/mappings/{id}/export.html` → `text/html; charset=utf-8`, attachment filename
    `review-<mapping>.html`.
- Static UI assets are served as `text/html`, `application/javascript`, or `text/css`.

| Status | Meaning |
|---|---|
| `400` | missing reviewer, malformed JSON, invalid field/id, or a business-rule refusal |
| `401` | bearer token required or mismatched |
| `404` | unknown API path or missing entity |
| `409` | `read_only` vendor write, or a connector's `UnsupportedOperation` |
| `411` | chunked request body without a usable length |
| `500` | unhandled server error; body is a generic message (no token/traceback) |
| `502` | vendor connector error, with its status/reason in the body |

`read_only` blocks only actions that leave this machine: sync jobs, retries, deploys,
per-component apply, and undo. Decisions, notes, mappings, refreshes, and license-map edits stay
local and writable.

## Local routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness, DB state, read-only flag, connection capabilities |
| GET | `/api/tools` | connector catalog and supported roles |
| GET | `/api/version` | product name and version |
| GET/POST | `/api/connections` | list/create connections; credentials are env-var names only |
| POST | `/api/connections/{id}/test` | connectivity test |
| GET | `/api/connections/{id}/projects` | PL project/version discovery |
| GET | `/api/connections/{id}/scans` | SL scan discovery |
| GET | `/api/connections/{id}/folders` | FOSSology folders, selectable as `folder:<id>` scans |
| GET | `/api/connections/{id}/capabilities` | connector read/write/verify flags |
| GET/POST | `/api/mappings` | list/create PL↔SL mappings |
| GET | `/api/mappings/{id}` | one mapping |
| DELETE | `/api/mappings/{id}` | delete the mapping and all its rows in one transaction (`?confirm=<exact name>`; 409 while refreshing; audit kept) |
| GET | `/api/mappings/{id}/usage` | rows a delete would remove, per table |
| POST | `/api/mappings/{id}/refresh` | retrieve both sides and recompute; decisions survive |
| GET | `/api/mappings/{id}/refresh-plan` | per-side work and a time estimate from the last refresh; `needs_approval` at 30 s or unknown |
| GET | `/api/mappings/{id}/overview` | counts, retrieval status, recent sync |
| GET | `/api/mappings/{id}/targets` | display name and tool link for the mapped PL project version and SL folder/upload |
| GET | `/api/mappings/{id}/components` | review queue (query: `q`, `status`, `review_status`, `confidence`, `sort`, `offset`, `limit`) |
| GET | `/api/components/{id}` | workspace detail |
| POST | `/api/components/{id}/decisions` | field decision; false-positive decisions accept `scope` and `path_pattern` |
| POST | `/api/components/{id}/notes` | reviewer note |
| POST | `/api/components/{id}/review` | review-state transition |
| POST | `/api/components/{id}/confirm` / `reassign` / `reject` | match management |
| POST | `/api/components/{id}/suggestions` | record machine output separately from decisions |
| POST | `/api/suggestions/{id}/decision` | accept/reject a suggestion through normal decision validation |
| POST | `/api/sync/preview` | approved-change preview; no reviewer required |
| POST | `/api/sync/jobs` | dry-run or live job (`dry_run`, `confirm`, `component_ids`, `finalize`) |
| GET | `/api/sync/jobs` | recent jobs (`mapping_id`, `limit`) |
| GET | `/api/sync/jobs/{id}` | per-component report |
| POST | `/api/sync/jobs/{id}/retry` | retry failed/interrupted items |
| POST | `/api/deploy` | BOM → id2url → FOSSology folder (`dry_run` defaults true) |
| GET | `/api/audit` | audit events |
| GET | `/api/mappings/{id}/export.csv` | full review report as CSV |
| GET | `/api/mappings/{id}/export.html` | full review report as HTML |
| GET/POST | `/api/license-map` | list/upsert FO short name → SPDX → BD license |
| DELETE | `/api/license-map/{id}` | delete a license mapping |
| GET | `/api/license-map/suggestions` | proposed entries from SL snapshot names, the PL catalogue and SPDX; reads only |
| POST | `/api/license-map/apply` | write accepted proposals in one transaction |
| GET | `/api/false-positive-rules` | remembered rule scopes and revocation state |
| POST | `/api/false-positive-rules/{id}/revoke` | revoke a rule so the finding surfaces again |
| GET | `/api/mappings/{id}/delta` | compare current PL version with the previous mapping |
| POST | `/api/mappings/{id}/delta/apply` | carry prior-version decisions into delta matches |
| GET | `/api/sync/jobs/{id}/undo/preview` | read restorable values and manual-action cases |
| POST | `/api/sync/jobs/{id}/undo` | restore prior PL/FO values where capabilities allow |
| POST | `/api/components/{id}/apply` | apply one approved component, then mark BD reviewed and close FO |

## Vendor headers, media types, and fallbacks

### Black Duck

Read requests use `Authorization: Bearer <token>`; request headers include the BOM or copyright
media type where the vendor requires it:
- BOM: `Accept: application/vnd.blackducksoftware.bill-of-materials-7+json`
- copyright: `Accept: application/vnd.blackducksoftware.copyright-4+json`
- writes: `Content-Type` matching the resource (`bill-of-materials-7+json` for BOM/license PUTs,
  `copyright-4+json` for copyright POSTs).

Fallbacks and refusals:
- A component with no writable BOM handle fails the sync item with a named reason; it is not written
  somewhere guessed.
- License declarations are best-effort: resolve `GET /api/licenses?q=name:...`, rebuild the
  component's `licenses[]`, PUT the BOM resource, and verify by re-read. A verification mismatch is
  `failed`.
- Copyrights are added per origin; only notices not already present are POSTed (a stored but inactive one is reactivated). A rejected PL-only notice is deactivated (`PUT` with `active: false`), never deleted. A component with no
  origin fails.
- License text PUT targets the component's own license resource; if several licenses remain ambiguous,
  the write fails with the candidate list rather than choosing one.
- Review-status finalization PUTs the BOM component with `reviewStatus: REVIEWED`; it is verified by
  re-read and only reached after every approved field write succeeds.

### FOSSology

Read requests use `Authorization: Bearer ...` or basic auth when configured; `Accept` is
`application/json`. Listing uses `page` (and `X-Total-Pages`) headers. Findings require the
`agent` query parameter.

Fallbacks and refusals:
- `412` ungrounded agent and `503` unpacking are connector errors, not "no findings"; refresh only
  after the agents run.
- Per-file copyrights fall back to aggregate `GET /uploads/{id}/copyrights` when the findings lack
  copyright rows.
- `GET /license/{shortName}` is canonical metadata text, labelled `generic_metadata` and never sent
  to PL; original source-file text is only approved when an identified file is readable.
- Folder scans are exposed as `folder:<id>`; one upload is treated as one library, matching deploy's
  upload naming.
- Upload creation sends `folderId` and `uploadType=url|vcs|file|server`, with `location` or
  `fileInput`, `mainLicense`, and `scanOptions`. Uploads are never retried.
- Closing an upload is `PATCH /uploads/{id}` with `status=Closed` and a `text/plain` body; the
  configured comment defaults to `reviewed`.

## Deploy fallbacks

Source URL resolution is ordered: id2url from purl/origin → SBOM `downloadLocation` → `originUrl`
→ operator-supplied VCS URL → `no_source_url`. Unknown registries are `unsupported_registry`;
dead URLs are `upload_failed`. id2url runs as a subprocess (AGPL-3.0), not an import.

## Configuration profiles

`config.yaml` defines `local` and `production` profiles. Credential blocks use environment-variable
names, never values. The PL connection's `config.folder_mapping` supplies the folder template,
overrides, sanitizer/regex rules used by deploy; the SL connection supplies `auto_close_fo`,
`close_comment`, `agents`, and deploy target keys. `clreview/config.py` loads this YAML subset and
`profile_connection()` converts a role block into the same shape accepted by `POST
/api/connections`.
