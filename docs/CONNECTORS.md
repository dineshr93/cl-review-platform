# Connectors

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
and truncate error bodies. Credentials are read from the environment variable named in the
connection's `auth_env`; tokens are never logged or returned.

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

Verified against `blackducksoftware/hub-rest-api-python`:

| Operation | Endpoint | Notes |
|---|---|---|
| connectivity | `GET /api/projects?limit=1` | bearer token |
| projects | `GET /api/projects` (paginated `items`/`totalCount`) | project id from `_meta.href` |
| versions | `GET /api/projects/{id}/versions` | `versionName` |
| BOM components | `GET /api/projects/{id}/versions/{vid}/components` | `componentName`, `componentVersionName`, `licenses`, `origins`, `_meta.href` |
| license declarations | nested `licenses[]`: `{licenseDisplay, license}` or `{licenseType, licenses[]}` | conjunctive/disjunctive preserved |
| license detail + text | license link → `name`, `spdxId`, `text` link | canonical KB text, labelled `generic_metadata` |
| copyrights | `component-origin-copyrights` link on each origin | `updatedCopyright`/`kbCopyright`, `active`; deduplicated |
| license write | `PUT` BOM component resource with rebuilt `licenses` array | license resolved by `GET /api/licenses?q=name:...`; verified by re-read |
| copyright write | — | **unsupported** (see LIMITATIONS) |
| license text write | — | **unsupported** (see LIMITATIONS) |

The connector never calls `PUT {origin}/copyrights-refresh` as a synchronization: that endpoint
only re-requests origin data and does not accept new copyright statements.

## FOSSology (SL)

Verified against `dineshr93/easy_fossy` and the FOSSology REST `openapi.yaml`:

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

`easy_fossy` exposes the copyright and per-file endpoints (`uploads.get_copyrights`,
`get_copyrights_by_upload_id_uploadtree_id`); this connector calls the REST endpoints directly so
the item-content and tree mapping stay explicit. `412` responses (agent not scheduled) surface as
connector errors rather than empty evidence.

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

Environment (`.env.example`):

```
BLACKDUCK_URL=https://blackduck.example
BLACKDUCK_TOKEN=            # Hub access token
FOSSOLOGY_URL=https://fossology.example
FOSSOLOGY_TOKEN=            # FOSSology API token, or:
FOSSOLOGY_USER=
FOSSOLOGY_PASS=
CLREVIEW_TOKEN=             # optional shared API token
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

`config.example.json` contains the same shapes plus a ScanCode example.
