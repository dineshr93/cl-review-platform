# CL Review — Copyright & License Data Review Platform

[![CI](https://github.com/dineshr93/cl-review-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/dineshr93/cl-review-platform/actions/workflows/ci.yml)
[![Release](https://github.com/dineshr93/cl-review-platform/actions/workflows/release.yml/badge.svg)](https://github.com/dineshr93/cl-review-platform/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Tool-agnostic review platform for copyright and license data. Source-level (SL) scanners provide
evidence; a human validates it; only explicitly approved corrections are written to the
project-level (PL) tool. Synchronization is **one-way, SL → PL**, never reverse.

```
PL tool (Black Duck / FOSSID)          SL tool (FOSSology / ScanCode)
        │  read components                     │  read components, copyrights, licenses, license files
        └──────────────┬───────────────────────┘
                       ▼
        snapshot → match → compare → human decision → preview → dry-run → write → verify → audit
```

Raw observations are stored separately from normalized comparison values and from approved values.
Refreshing a snapshot never erases a review decision; if source data changes after review, the
decision is flagged **stale** and must be re-reviewed before it can be synchronized.

## Quick start (no dependencies)

Python 3.10+ standard library only — no pip install, no database server.

```bash
python3 -m clreview.server --demo
# open http://127.0.0.1:8765
```

`--demo` seeds two mock connections (a project-level and a source-level tool), one project
mapping, and a realistic fixture set: identical components, a fuzzy match suggestion, unmatched
components on both sides, a copyright formatting variant, a license detected only in SL, a
generic license text in PL versus an original `LICENSE` file in SL, and a false-positive license.

Explore: **Project overview** → **Review queue** → open a component → decide fields →
**Approve component for synchronization** → **Synchronization history** → **Dry run** →
tick *I approve these writes* → **Execute synchronization**.

Other commands:

```bash
python3 -m clreview.server --db clreview.db --port 8765     # empty instance, configure real tools
python3 -m unittest discover -s tests -t tests -v           # 63 tests
python3 -m clreview.server --version
```

Install as a package (from a release artifact or the source tree):

```bash
python3 -m pip install cl_review_platform-1.0.0-py3-none-any.whl   # wheel from Releases
# or
python3 -m pip install .
clreview-server --demo
```

Docker:

```bash
docker build -t cl-review-platform .
docker run --rm -p 127.0.0.1:8765:8765 -v clreview-data:/data \
  -e CLREVIEW_TOKEN=change-me cl-review-platform
# or
docker compose up --build
```

The image runs as a non-root user, persists SQLite in `/data`, and exposes a `/api/health`
healthcheck. `docker-compose.yml` binds the port to loopback by default.

## Production deployment

- Bind to loopback behind a TLS reverse proxy, or set `CLREVIEW_TOKEN` and send
  `Authorization: Bearer <token>`. The server refuses nothing, but it warns loudly when bound
  beyond loopback without a token.
- Put reviewer identity in `X-Reviewer` (validated and recorded on decisions, notes, audit).
- Persist the database on a volume and back it up (SQLite file plus `-wal`/`-shm` while stopped, or
  use `sqlite3 .backup`). One server process per database file.
- Configure connections with credential **environment variable names**, not values. Rotate tokens
  by changing the environment; restart the server.
- Probe `/api/health` (public, returns DB status and version) from your orchestrator.
- Logs go to stderr (`--log-level debug|info|warning|error`); request lines contain no credentials.
- Shutdown is graceful on `SIGTERM`/`SIGINT`; the HTTP server stops accepting connections and closes
  the database.
- Keep the synchronization item cap (default 500) as a bulk-overwrite guard; live jobs still need
  `confirm=true` and a tested PL connection.

## Real integrations

Connections are configured in the UI (or via `POST /api/connections`). Credentials are **never**
stored in the database or returned by the API — only the name of the environment variable that
holds them. Copy `.env.example` and export the variables before starting the server.

```bash
export BLACKDUCK_URL=https://blackduck.example
export BLACKDUCK_TOKEN=...            # Hub access token
export FOSSOLOGY_URL=https://fossology.example
export FOSSOLOGY_TOKEN=...            # FOSSology API token (or FOSSOLOGY_USER/FOSSOLOGY_PASS)
python3 -m clreview.server --db clreview.db
```

Then in the UI:

1. **Add connection** — role `pl`, tool `blackduck`, base URL, token env `BLACKDUCK_TOKEN`.
2. **Add connection** — role `sl`, tool `fossology`, base URL, token env `FOSSOLOGY_TOKEN`.
3. **Test** each connection (synchronization stays disabled for a PL connection that has not
   passed a connectivity test).
4. **Create mapping** — choose PL project/version and the SL upload/scan.
5. **Refresh snapshots** and review.

Example JSON configuration lives in `config.example.json`; the API equivalent is documented in
[docs/CONNECTORS.md](docs/CONNECTORS.md).

## What the reviewer gets

- **Project overview**: connection health, retrieval timestamps and errors, component/match
  counts, pending review, approved-for-sync, stale approvals, recent sync jobs.
- **Review queue**: search by component name, version, or license; filter by match status,
  confidence, review status; sort by name/confidence/discrepancies; paginated.
- **Component workspace**: PL and SL identity with match evidence and mismatch reasons,
  side-by-side copyright notices, side-by-side license names with SPDX and evidence paths,
  license text viewer with provenance (generic metadata vs original source file), per-entry
  decisions, notes, stale-approval warnings, approved synchronization preview.
- **Synchronization history**: dry-run and live jobs, before/after values, API status,
  verification result, per-component report, retry of failed items, audit trail.

## Review lifecycle

Review status is computed per component from field-level decisions:

`not_reviewed` → `review_required` → `in_progress` → `reviewed` → `approved_for_sync`

Synchronization status is tracked separately per job and per item:

`pending` → `dry_run` / `in_progress` → `synchronized` | `failed` | `skipped` | `unsupported`

Item statuses: `ready`, `already_applied`, `unsupported_target`, `conflict`, `stale`,
`succeeded`, `failed`, `skipped_already_applied`.

## Matching

Hierarchy, with confidence and rationale recorded for every proposal:

1. identical normalized purl — 0.99
2. identical normalized name + version — 0.95
3. shared strong identifier — 0.90
4. name match with a version missing on one side — 0.72
5. fuzzy name similarity (≥ 0.72) — **suggestion only**, confidence < 0.70, requires confirmation

Weak fuzzy matches never merge components automatically. Unmatched components stay visible on
both sides and can be manually assigned, confirmed, or rejected. Rejections survive refreshes.

## License text provenance

PL text is classified as `generic_metadata` (canonical KB text) or `source_file` (component-specific).
SL generic metadata (`/license/{shortName}`) is labelled as metadata and is **never** sent to PL.
Only text read from an identified license file in the source tree (`LICENSE`, `LICENCE`, `LICENSE.txt`,
`COPYING`, `COPYING.LESSER`, `NOTICE`, case/extension variants, and equivalents discovered in the tree)
can be approved for synchronization. If no reliable source text exists, the component is flagged
for further review — no text is invented or reconstructed.

## Synchronization rules

- Only decisions with an explicit approval (`approve_sl` / `correct`) on a component that is
  `approved_for_sync` are candidates.
- Live writes require `confirm=true`, a verified PL connection, and stay below a configurable
  item limit (default 500) as bulk-overwrite protection.
- Each item is validated against the connector's declared capability; unsupported fields are
  reported as `unsupported_target` with the reason, never reported as success.
- Every write records the API status/response and is verified by re-reading PL where the API allows it.
- Re-running is idempotent: values already present are `skipped_already_applied`.
- Decisions made against older source versions are `stale`; PL values that changed after review are
  `conflict`; neither is written.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness |
| GET | `/api/tools` | connector catalog and supported roles |
| GET/POST | `/api/connections` | list/create connections |
| POST | `/api/connections/{id}/test` | connectivity test |
| GET | `/api/connections/{id}/projects` | PL project + version discovery |
| GET | `/api/connections/{id}/scans` | SL scan discovery |
| GET/POST | `/api/mappings` | list/create PL↔SL mappings |
| POST | `/api/mappings/{id}/refresh` | retrieve both sides, re-match, preserve decisions |
| GET | `/api/mappings/{id}/overview` | counts, side status, recent sync |
| GET | `/api/mappings/{id}/components` | queue (`q`, `status`, `review_status`, `confidence`, `sort`, `offset`, `limit`) |
| GET | `/api/components/{id}` | workspace detail |
| POST | `/api/components/{id}/decisions` | field-level decision |
| POST | `/api/components/{id}/notes` | reviewer note |
| POST | `/api/components/{id}/review` | review state transition |
| POST | `/api/components/{id}/confirm` / `reassign` / `reject` | match management |
| POST | `/api/components/{id}/suggestions` | record a machine suggestion |
| POST | `/api/suggestions/{id}/decision` | accept/reject a suggestion |
| POST | `/api/sync/preview` | approved-change preview |
| POST | `/api/sync/jobs` | dry-run or live job (`dry_run`, `confirm`, `component_ids`) |
| GET | `/api/sync/jobs/{id}` | per-component report |
| POST | `/api/sync/jobs/{id}/retry` | retry failed items |
| GET | `/api/audit` | audit events |

Optional shared-token auth: set `CLREVIEW_TOKEN`; clients send `Authorization: Bearer <token>`.
The reviewer identity comes from `X-Reviewer` and is stored on decisions, notes, and audit events.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — layers, domain model, data flow, statuses.
- [docs/CONNECTORS.md](docs/CONNECTORS.md) — connector contract, per-tool API mapping, adding a tool.
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md) — verified API limitations and how they are handled.
- [docs/ROADMAP.md](docs/ROADMAP.md) — FOSSID/ScanCode status, LLM assistance design.
- [SECURITY.md](SECURITY.md) — vulnerability reporting and deployment constraints.

## Releases

Tags matching `v*` trigger `.github/workflows/release.yml`, which builds the sdist and wheel,
verifies the wheel contents and entry point, writes `dist/SHA256SUMS.txt`, and creates a GitHub
release with generated notes and the artifacts. Manual runs accept an existing tag. See
[CHANGELOG.md](CHANGELOG.md).

## Testing

63 stdlib tests cover matching, comparison, review decisions, stale data, unsupported writes,
API failures and retries, idempotent re-sync, connector error handling, credential hygiene, and an
end-to-end HTTP workflow against a mock PL. No production credentials are needed.

## Security posture

- Credentials only in environment variables; configs are sanitized before being returned.
- Read and write paths are separate connector methods; writes require explicit approval and confirmation.
- No success status until the target API operation succeeded and (where possible) was verified.
- Input validation on every write endpoint; bounded request bodies; no stack traces or tokens in responses.
- SQLite persistence; single-writer local deployment. For multi-user deployments, put the API behind
  an authenticating reverse proxy and use per-user reviewer identity.
