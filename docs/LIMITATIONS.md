# Verified limitations and handling

Nothing below is invented. Each limitation was checked against the reference repositories
(`blackducksoftware/hub-rest-api-python`, `dineshr93/easy_fossy` + FOSSology REST `openapi.yaml`)
and is isolated behind the connector contract.

## Black Duck

### Copyright statements cannot be written

`component-origin-copyrights` is a read-only origin resource. The only write-shaped operation in
the reference repo is `PUT {origin}/copyrights-refresh`
(`examples/client/refresh_copyrights.py`, `refresh_project_copyrights.py`), which asks Black Duck
to re-fetch origin data; it does not accept replacement text.

Handling: `Capabilities.write_copyrights = False`. Sync preview marks copyright items
`unsupported_target` with the reason. Copyright review and decisions are still fully supported and
audited; only the PL write is impossible through this API. A mock/demo PL declares the capability
so the full workflow remains demonstrable.

### Component-specific license text cannot be written

The license detail exposes a `text` link to the canonical KB text
(`blackduck/Licences.py::_get_license_info`). There is no documented endpoint to store an original
license file's text on a component version.

Handling: PL text is read and classified as `generic_metadata`; `write_license_text = False`;
the platform never substitutes generic metadata text for original text.

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
- FOSSology exposes copyright editing endpoints (`user-copyrights`), but the platform only reads
  from SL: synchronization is one-way SL → PL.

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
