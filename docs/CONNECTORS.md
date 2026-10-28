# Connectors

Endpoint mappings below are audited against the vendor specs checked into `apis/`
(`blackduck_Blackduck-REST-API-v2026.4.2.md`, `fossology_openapi.yaml`). The full read/write matrix
for both tools, including the endpoints this platform deliberately does not call, is in
[API-CAPABILITIES.md](API-CAPABILITIES.md).

## Contract

`clreview/connectors/base.py`

Project-level (`pl`):

```python
class ProjectLevelConnector(Protocol):
    tool: str
    capabilities: Capabilities
    def test_connection(self) -> dict: ...
    def list_projects(self) -> list[ProjectInfo]: ...
    def list_versions(self, project_id: str) -> list[str]: ...
    def fetch_inventory(self, project_id: str, version_id: str) -> list[ComponentData]: ...
    def read_component(self, key: str) -> dict: ...          # current PL values, for verification
    def update_licenses(self, key: str, entries: list[dict]) -> dict: ...
    def update_copyrights(self, key: str, notices: list[str]) -> dict: ...
    def update_license_text(self, key: str, text: str, file_name=None) -> dict: ...
```

Source-level (`sl`): `test_connection`, `list_scans`, `fetch_inventory(scan_id)`.
Writes on an SL connector raise `UnsupportedOperation` — synchronization is one-way SL → PL.

`Capabilities` declares what is actually possible:

```python
Capabilities(read_components=True, read_copyrights=True, read_license_text=True,
             write_copyrights=False, write_license_names=True, write_license_text=False,
             verify_writes=True, notes="...")
```

The sync layer checks these flags before writing and reports `unsupported_target` with the
capability note instead of pretending success.

`ComponentData.write_key` carries the handle a PL write needs (Black Duck BOM component URL,
mock `project|version|external_id`). `None` means the connector exposes no writable handle; sync
fails the item with a clear message.

Errors: `ConnectorError(message, status=..., unsupported=...)` and `UnsupportedOperation`.
HTTP helpers retry transient statuses (429/500/502/503/504) with bounded backoff, honor timeouts,
and truncate error bodies. A write is never retried — a Black Duck `POST`/`PATCH` and a FOSSology
upload POST go out once, because retrying a write that already landed would duplicate it.
Credentials are read from the environment variable named in the
connection's `auth_env`; tokens are never logged or returned.

### Connection config keys

`config` is a free-form JSON object stored on the connection (never secret: a key naming a
credential is rejected at creation). Each connector reads the keys it knows, with these defaults:

| Tool | Key | Default | Meaning |
|---|---|---|---|
| blackduck | `base_url` | connection's base URL | fallback used only when the connection has no base URL |
| blackduck | `timeout` | `30` | seconds per request |
| blackduck | `retries` | `2` | retries for **reads** on 429/500/502/503/504; writes are not retried |
| fossology | `base_url` | connection's base URL | as above |
| fossology | `agents` | `nomos` | comma-separated findings agents to read |
| fossology | `max_license_files` | `10` | license files per component whose content is fetched |
| fossology | `generic_text_limit` | `3` | licenses per component whose generic metadata text is looked up |
| fossology | `timeout` / `retries` | `30` / `2` | as above (reads only) |
| fossology | `folders_as_scans` | `true` | offer folders as `folder:<id>` scans |
| scancode | `json_path` | required | path to a `--json-pp` file; may be named in `auth_env.json_path` |
| mock-pl / mock-sl | `fixtures` | packaged `clreview/fixtures/demo` | fixture file or directory |
| blackduck (deploy) | `id2url_command` | `id2url` | path to the id2url binary, run as a subprocess |
| fossology (deploy) | `deploy_parent_folder` | none | folder id a deploy creates `{project}__{version}` under |

## Adding a connector

1. Create `clreview/connectors/mytool.py` implementing the protocol and capabilities.
2. Register it:

```python
REGISTRY["mytool"] = {
    "roles": ("pl",),
    "factory": _factory("clreview.connectors.mytool.MyToolConnector"),
    "description": "MyTool REST API (components, licenses)",
}
```

3. Configure it through `POST /api/connections` or the UI. No change to matching, review, sync,
   audit, or the UI is required.

`create_connector(tool, role, ...)` rejects unknown tools and roles the tool does not declare.

## Black Duck (PL)

Verified against `apis/blackduck_Blackduck-REST-API-v2026.4.2.md` and
`blackducksoftware/hub-rest-api-python`:

| Operation | Endpoint | Notes |
|---|---|---|
| connectivity | `GET /api/projects?limit=1` | bearer token |
| projects | `GET /api/projects` (paginated `items`/`totalCount`) | project id from `_meta.href` |
| versions | `GET /api/projects/{id}/versions` | `versionName` |
| BOM components | `GET /api/projects/{id}/versions/{vid}/components` | `componentName`, `componentVersionName`, `licenses`, `origins`, `_meta.href` |
| license declarations | nested `licenses[]`: `{licenseDisplay, license}` or `{licenseType, licenses[]}` | conjunctive/disjunctive preserved |
| license detail + text | license link → `name`, `spdxId`, `text` link | canonical KB text, labelled `generic_metadata` |
| copyrights | copyright collection on each origin: `…/origins/{oid}/copyrights` | `updatedCopyright`/`kbCopyright`, `active`; deduplicated |
| license write | `PUT` BOM component resource with rebuilt `licenses` array | license resolved by `GET /api/licenses?q=name:...`; verified by re-read |
| copyright write | `POST …/components/{cid}/versions/{cvid}/origins/{oid}/copyrights` `{"copyright": …}` | stored with `source: CUSTOM`; additive — existing statements are never edited or deactivated |
| license text write | `PUT …/components/{cid}/licenses/{lid}/text` `{"text": …}` (`…bill-of-materials-7+json`) | component-specific override of the KB text; the license is picked from the BOM component's own `licenses[]` |
| deploy coordinates | same BOM call, identity only (`source_coordinates`) | `packageUrl` / `externalNamespace:externalId` per component, no license calls — the input of `id2url` |

The connector never calls `PUT {origin}/copyrights-refresh` as a synchronization: that endpoint
only re-requests origin data and does not accept new copyright statements. It also never writes the
shared knowledge base (`POST/PUT /api/licenses`, `PUT /api/licenses/{id}/text`) — a review conclusion
belongs to the component, not to the KB.

## FOSSology (SL)

Verified against `apis/fossology_openapi.yaml` and `dineshr93/easy_fossy`:

| Operation | Endpoint | Notes |
|---|---|---|
| connectivity | `GET /uploads?limit=1` | bearer token or basic auth |
| scans | `GET /uploads` (header `page`, `X-Total-Pages`) | `id`, `uploadname`, `uploaddate` |
| per-file findings | `GET /uploads/{id}/licenses?agent=nomos&copyright=true` | `filePath`, `findings.scanner`, `findings.copyright` |
| upload name | `GET /uploads/{id}/summary` | `uploadName` |
| item content | `GET /uploads/{id}/item/{itemId}/view` | original file text (source-file evidence) |
| item ids | `GET /uploads/{id}/topitem` + `GET /uploads/{id}/item/{itemId}/tree/view` | recursive path→id map, cached |
| generic license text | `GET /license/{shortName}` | canonical metadata, labelled `generic_metadata` |
| aggregate copyrights | `GET /uploads/{id}/copyrights` | fallback when per-file findings lack copyrights |
| folder list / create | `GET /folders`, `POST /folders` (headers `parentFolder`, `folderName`) | 200 = existed, 201 = created; the id is re-read from the listing |
| uploads in a folder | `GET /uploads?folderId=&recursive=false` | deploy idempotency check (by upload name) |
| upload a source archive | `POST /uploads` (headers `folderId`, `uploadType=url`) | `{location:{url,name}, scanOptions.analysis.*}`; never retried |

`easy_fossy` exposes the copyright and per-file endpoints (`uploads.get_copyrights`,
`get_copyrights_by_upload_id_uploadtree_id`); this connector calls the REST endpoints directly so
the item-content and tree mapping stay explicit. `412` responses (agent not scheduled) surface as
connector errors rather than empty evidence.

FOSSology is writable over the same REST API — `POST /folders`, `POST /uploads`
(`file`/`vcs`/`url`/`server` + `mainLicense` + `scanOptions`), `POST /jobs`, `PUT …/item/{id}/licenses`,
`POST …/licenses/main`, `PUT …/item/{id}/clearing-decision`, `PUT …/item/{id}/user-copyrights/{hash}`.
The deploy step uses two of them (`POST /folders`, `POST /uploads`); the rest stay unused, because
review conclusions belong to Black Duck, not to the scan. The full write surface is documented in
API-CAPABILITIES.md.

A FOSSology folder is also offered as a scan (`folder:<id>`, named `… (folder)`), so a whole
deployed Black Duck project version becomes one mapping. Inside a folder each upload is one
component, named from the upload name the deployer chose.

Component grouping heuristic: if the upload has at most one top-level directory it is one
component (name/version parsed from the directory or upload name); otherwise each top-level
directory is a component and files at the root belong to the upload-named component. Configure
`agents` (default `nomos`), `max_license_files` (default 10), `generic_text_limit` (default 3).

## ScanCode Toolkit (SL)

Reads a `--json-pp` output file (`config.json_path` or its env variable). Packages are used when
present (`package_name`, `package_version`, `purl`, `license_expression`); otherwise files are
grouped like FOSSology. Copyrights come from `files[].copyrights`; license names from
`license_detections[].license_expression`. Source-file license text is only available when the file
record carries `text`/`file_content`; otherwise the component is flagged for review.

## FOSSID (PL, placeholder)

Registered with role `pl` and capabilities all `False`; every operation raises a clear
`ConnectorError(..., unsupported=True)`. No API mapping is invented. See ROADMAP for the adapter plan.

## Mock connectors (demo/tests)

`mock-pl` and `mock-sl` load JSON fixtures, declare full write capability for `mock-pl`, simulate a
one-shot API failure (`fail_writes_once`), and reload fixtures when the file changes while keeping
in-memory writes. They back the demo mode and the end-to-end test.

## Configuration examples

Environment (`.env.example`). The first block is what the server itself reads; the second is the
names a connection's `auth_env` points at — no code looks those up by name, and a tool's URL is the
connection's `base_url`, never an environment variable:

```
CLREVIEW_DB=clreview.db
CLREVIEW_HOST=127.0.0.1
CLREVIEW_PORT=8765
CLREVIEW_LOG_LEVEL=info
CLREVIEW_TOKEN=             # optional shared API token

BLACKDUCK_TOKEN=            # Hub access token
FOSSOLOGY_TOKEN=            # FOSSology API token, or:
FOSSOLOGY_USER=
FOSSOLOGY_PASS=
SCANCODE_JSON=              # path to a --json-pp output file
```

`POST /api/connections`:

```json
{
  "role": "pl",
  "tool": "blackduck",
  "name": "Prod Black Duck",
  "base_url": "https://blackduck.example",
  "config": {"timeout": 30, "retries": 3},
  "auth_env": {"token": "BLACKDUCK_TOKEN"}
}
```

```json
{
  "role": "sl",
  "tool": "fossology",
  "name": "Prod FOSSology",
  "base_url": "https://fossology.example",
  "config": {"agents": "nomos", "max_license_files": 10, "timeout": 30, "retries": 2},
  "auth_env": {"token": "FOSSOLOGY_TOKEN"}
}
```

`config.example.json` shows the same shapes plus a ScanCode example. It is documentation of the
request bodies — no code reads that file; configuration is the CLI, the environment, and the
`POST /api/connections` body.

Deploy keys (`POST /api/deploy`): the PL connection may set `id2url_command` — path to the id2url
binary, default `id2url` on `PATH`; the SL connection may set `deploy_parent_folder` — the folder id
deploy creates `{project}__{version}` under when the request does not name one — and
`folders_as_scans` (default `true`, list FOSSology folders among the selectable scans).
