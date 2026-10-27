# Verified limitations and handling

Nothing below is invented. Each limitation was checked against the vendor specifications in `apis/`
(`blackduck_Blackduck-REST-API-v2026.4.2.md`, `fossology_openapi.yaml`) and the reference
repositories (`blackducksoftware/hub-rest-api-python`, `dineshr93/easy_fossy`), and is isolated
behind the connector contract. The full endpoint matrix is in [API-CAPABILITIES.md](API-CAPABILITIES.md).

## Black Duck

### Copyright writes are additive, per origin, and never destructive

Black Duck stores copyright statements on a component-version **origin**
(`POST/PUT …/components/{cid}/versions/{cvid}/origins/{oid}/copyrights`, `source: CUSTOM`). A BOM
component can carry several origins, and the reference documents no delete for a copyright — only
create, update, bulk update, and `active: false`.

Handling: `write_copyrights = True`. The connector POSTs only the statements Black Duck does not
already have, to the first origin of the component, and never edits or deactivates an existing one.
A value removed during review therefore stays in Black Duck instead of being silently destroyed;
the sync record shows exactly what was created (`body.created`). A component with no origin fails
the item with a named reason rather than writing to a guessed target.

### Component license text needs an unambiguous license

`PUT …/components/{cid}/licenses/{lid}/text` stores a component-specific license text (and
`DELETE` on the same resource restores the KB text). The target license id comes from the BOM
component's own `licenses[]`.

Handling: `write_license_text = True`. When the component declares exactly one license the write
goes to it. When it declares several, the connector matches the reviewed file name against the
license names and, if that is still ambiguous, fails the item with the candidate list instead of
picking one. The restore endpoint is deliberately not called: reverting a component's text to the
KB default is a human decision, not a side effect of synchronization.

### License declarations: best-effort write, verified

The vendor example `examples/client/update_project_with_component_licenses.py` PUTs a BOM component
resource with a rebuilt `licenses` array, demonstrated for `SUB_PROJECT` components. Whether a
KB component accepts the same PUT depends on the instance and API version.

Handling: `write_license_names = True` but treated as best-effort. The connector resolves the
target license via `GET /api/licenses?q=name:...` (fails the item if the name is unknown), PUTs the
component resource, and re-reads it for verification. A rejected PUT or a verification mismatch is
recorded as `failed`, never as success.

### Timeouts on large copyright sets

`refresh_project_copyrights.py` notes read timeouts for components with large copyright data
(e.g. Linux kernel). Handling: configurable `timeout`/`retries` per connection and bounded retries
on 429/500/502/503/504. A timeout is an integration error shown in the overview, not a silent
empty result.

## FOSSology

- The per-file findings endpoint `/uploads/{id}/licenses` requires an `agent` query parameter
  (`nomos`, `monk`, `ninka`, `ojo`, `reportImport`, `reso`, `scancode`). A `412` means the agent was
  not scheduled; `503` means the unpack agent has not started. Both surface as connector errors.
- `filePath` values come from the findings endpoint; item content requires resolving the tree
  (`/uploads/{id}/topitem` then `/uploads/{id}/item/{itemId}/tree/view`). That mapping costs one
  call per directory and is cached per upload. If a path cannot be resolved, the candidate file is
  skipped rather than guessed.
- `/license/{shortName}` returns canonical license metadata text, not the file's text. It is
  labelled `generic_metadata` and excluded from synchronization.
- FOSSology can **edit** a copyright finding (`PUT …/item/{id}/user-copyrights/{hash}`) but the REST
  API exposes no create: the collection is `GET`-only and the write verbs hang off a hash an agent
  already produced. A statement no scanner found cannot be added through the API.
- FOSSology is writable in general (folders, uploads, agent scheduling, license and clearing
  decisions). This platform writes exactly two things — a folder and an upload — during deploy, and
  never concludes a license, copyright, or clearing decision inside FOSSology. Review conclusions
  belong to Black Duck; synchronization stays one-way SL → PL.

## Deploy (Black Duck → FOSSology)

- `id2url` must be installed on the server (`uv pip install id2url`, or point the PL connection's
  `id2url_command` at the binary). It covers maven, npm, pypi, crates, nuget, rubygems, go,
  packagist, cocoapods, hex and pub; a component from any other registry is reported
  `unsupported_registry` and is never guessed at.
- Deploy does not wait for FOSSology's agents. Reading findings straight after an upload yields
  `412`/`503`, which the connector reports as an error, not as "no findings". Poll
  `GET /jobs/{id}` / `GET /uploads/{id}/agents` (or simply wait) before refreshing the mapping.
- Re-runs skip by **upload name**, not by content hash. A component whose archive changed without
  its name changing is not re-uploaded; a renamed archive is uploaded again.
- A folder scan assumes one upload = one library (that is what deploy creates). An upload holding
  several libraries inside one archive is still grouped by the ordinary directory heuristic.
- FOSSology performs the download, so a URL that dies between resolution and fetch fails as
  `upload_failed` with the URL in the report; unpack failures (`.nupkg`, `.crate`, platform `.gem`)
  surface later, in FOSSology, not in the deploy report.

## ScanCode Toolkit

`--json-pp` output carries paths, license detections and copyrights. It does not carry file
contents by default, so original license text is only available when the file record includes
`text`/`file_content`. Otherwise the component is flagged `no_source_text` and requires further
review; text is never reconstructed.

## FOSSID

Registered as a `pl` tool with all capabilities `False`; every method raises an explicit
not-implemented error. No endpoint mapping is claimed until verified against a live instance.

## LLM assistance

No model is called by this release. The `suggestions` table and endpoints accept machine output
with evidence/confidence/model identity, but acceptance goes through normal human decision
validation, and synchronization reads only decisions. A machine cannot approve or synchronize.
