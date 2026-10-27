# API capability audit — Black Duck ⇄ FOSSology

Audited from the vendor specifications checked into `apis/`:

| file | artifact | version |
|---|---|---|
| `apis/blackduck_Blackduck-REST-API-v2026.4.2.md` | Black Duck REST API reference | 2026.4.2 (42,066 lines) |
| `apis/blackduck_all-properties.md` | Black Duck representation property index | — |
| `apis/blackduck_using-environment-variables.md` | Black Duck env-var client config | — |
| `apis/fossology_openapi.yaml` | FOSSology REST OpenAPI | 217 KB, 119 paths |
| external | [`dineshr93/id2url`](https://github.com/dineshr93/id2url) README + CLI contract | registry origin id → source download URL; 11 registries; AGPL-3.0 |

Every row below is an endpoint that exists in one of those files. Nothing here is inferred from
blog posts or SDK wrappers. Line numbers point into the checked-in files so a future reader can
re-check without re-downloading anything.

## What this changed in the baseline

Three earlier "verified limitations" were wrong — the specs document the writes:

| previous claim | actual capability | spec location |
|---|---|---|
| Black Duck copyright statements cannot be written | `POST`/`PUT` on the component-version-origin copyright collection (`source: CUSTOM`) | BD md 23303 (create), 23365 (update), 23438 (bulk update) |
| Black Duck component license text cannot be written | `PUT …/components/{c}/licenses/{l}/text` stores a component-specific text; a restore endpoint reverts to KB text | BD md 10421, 10496, 10546, 10562 |
| FOSSology is read-only for this platform | folders, uploads, agent scheduling, license decisions, main license, clearing decisions and copyright edits are all REST-writable | FOSSY yaml 785, 3647, 3275, 1606, 1699, 1431, 2847 |

`Capabilities` for the Black Duck connector now reports `write_copyrights=True` and
`write_license_text=True`; the sync layer already routed those fields, so no sync change was needed.

## Black Duck — read surface used by the platform

| purpose | endpoint | notes |
|---|---|---|
| projects / versions | `GET /api/projects`, `GET /api/projects/{p}/versions` | paginated `items`/`totalCount` |
| BOM components | `GET /api/projects/{p}/versions/{v}/components` | `licenses[]`, `origins[]`, `inputExternalIds`, `_meta.href` = write handle |
| BOM component | `GET {bom component href}` | `licenses[].license` points at the **BOM component license** resource, not the KB license |
| origin copyrights | `GET /api/components/{c}/versions/{cv}/origins/{o}/copyrights` | `updatedCopyright`, `kbCopyright`, `active`, `source ∈ {KB, KB_MODIFIED, CUSTOM}`, `fileSha1s[]` |
| per-file copyright data | `GET …/origin/{o}/file-copyrights` (BD md 22286) | file-level evidence from Black Duck's own scan — a second opinion to FOSSology, not yet used |
| per-file license data | `GET …/file-licenses` per origin (BD md 22422, 22528, 22662) | same idea, license findings per file |
| KB license + canonical text | `GET /api/licenses?q=name:…`, license `text` link | canonical text is `generic_metadata` |
| BOM component license text | `GET …/components/{c}/licenses/{l}/text` | the component-specific text, i.e. what a review would write |
| origin ⇄ purl mapping | `GET …/components/{c}/origin-purls` (BD md 9111, 9161) | `origin` + `packageUrl` pairs — the join key for FOSSology uploads |
| origin detail | `GET /api/components/{c}/versions/{cv}/origins/{o}` | `packageUrl`, `originUrl`, `externalId` |
| source download URL | project version / component version SBOM field `downloadLocation` (BD md 3623, 19794) | per-package "where it came from" |
| whole-BOM export | `POST /api/projects/{p}/versions/{v}/sbom-reports` (BD md 27528) | SPDX 2.2/2.3/3.0, CycloneDX 1.3–1.6; template fields include `downloadLocation`, `purl`, `copyrights` |
| matched source file content | `GET /api/file-source-contents/{sha1}` (BD md 32813) | original text of a matched file, straight from Black Duck |
| scan archive | `GET /api/scan/data/{documentId}.bdio` (BD md 29379) | exported scan file |

## Black Duck — write surface

| field | endpoint | body | status |
|---|---|---|---|
| license declaration | `PUT {bom component href}` with rebuilt `licenses[]` | `{licenseDisplay, license}` | used; best-effort, verified by re-read |
| copyright statement (new) | `POST /api/components/{c}/versions/{cv}/origins/{o}/copyrights` | `{"copyright": "…"}` → stored `source: CUSTOM` | **implemented** |
| copyright statement (edit) | `PUT …/copyrights/{copyrightId}`; bulk `PUT …/copyrights` (each item needs `_meta.href`) | `{updatedCopyright, active, source}` | available, not used — the platform adds, never overwrites |
| copyright refresh | `PUT …/copyrights-refresh` | — | re-requests origin data only; never used as a sync |
| component license text | `PUT …/components/{c}/licenses/{l}/text` (and the `…/versions/{cv}/…` variant) | `{"text": "…"}` | **implemented** |
| component license text reset | `DELETE …/components/{c}/licenses/{l}/text` (BD md 10546, 10562) | — (204; removes the custom text and restores the KB text) | available, not used |
| KB license + its text | `POST/PUT /api/licenses`, `PUT …/licenses/{l}/text` (BD md 20465, 20688) | admin scope | deliberately out of scope — review conclusions belong to the component, not the KB |
| BOM SBOM fields | `PUT/PATCH …/components/{c}/sbom-fields` (BD md 13459–13643) | `purl`, `supplier`, `downloadLocation`, … | available for the FOSSology→Black Duck direction |
| BOM comment | `POST …/components/{c}/comments` (BD md 11654) | text | available as an audit trail next to a synced value |

Media types matter: copyright resources speak `application/vnd.blackducksoftware.copyright-4+json`,
BOM resources `…bill-of-materials-7+json`. The connector sends both explicitly.

## FOSSology — read surface used by the platform

| purpose | endpoint |
|---|---|
| scans (uploads) | `GET /uploads?folderId=&recursive=&status=` (`page` header, `X-Total-Pages`) |
| per-file findings | `GET /uploads/{id}/licenses?agent=nomos&copyright=true` |
| aggregate copyrights | `GET /uploads/{id}/copyrights` |
| per-file copyrights / emails / urls / authors | `GET /uploads/{id}/item/{i}/{copyrights,user-copyrights,scancode-copyrights,emails,urls,authors}` |
| file content | `GET /uploads/{id}/item/{i}/view` |
| tree | `GET /uploads/{id}/topitem`, `GET /uploads/{id}/item/{i}/tree/view` |
| summary / progress | `GET /uploads/{id}/summary`, `/clearing-progress`, `/licenses/histogram` |
| concluded vs scanned | `GET /uploads/{id}/licenses/edited`, `/licenses/scanned`, `/licenses/reuse` |
| agent state | `GET /uploads/{id}/agents`, `/agents/revision` |
| generic license text | `GET /license/{shortName}` |
| search | `GET /search?filename=&license=&copyright=&uploadId=` |
| folders | `GET /folders` |

## FOSSology — write surface (available; the platform uses two of these)

Deploy uses `POST /folders` and `POST /uploads` and nothing else on this list — the review loop
reads findings and never concludes licenses or copyrights inside FOSSology.

| purpose | endpoint | payload |
|---|---|---|
| create folder | `POST /folders` | headers `parentFolder`, `folderName`, `folderDescription`; 200 = already exists, 201 = created |
| upload a package | `POST /uploads` (header `folderId`, `uploadType=file\|vcs\|url\|server`) | `location` = `VcsUpload` \| `UrlUpload` \| `ServerUpload`, or `fileInput` binary; plus `mainLicense` and `scanOptions` |
| run agents later | `POST /jobs` (headers `folderId`, `uploadId`) | `ScanOptions` (`analysis.nomos/monk/…`, `decider.*`, `reuse.*`, `scancode.*`) |
| one-shot scan, no storage | `POST /uploads/oneshot/nomos\|monk\|ceu` | `fileInput` |
| conclude a file license | `PUT /uploads/{id}/item/{i}/licenses` | array of `{shortName, add, text, ack, comment}`; returns per-item `success`/`errors` |
| main license | `POST /uploads/{id}/licenses/main` / `DELETE /uploads/{id}/licenses/{shortName}/main` | `{shortName}` |
| clearing decision | `PUT /uploads/{id}/item/{i}/clearing-decision` | `{decisionType: 0\|3\|4\|5\|6\|7, globalDecision}` |
| edit a copyright finding | `PUT /uploads/{id}/item/{i}/user-copyrights/{hash}` (`{content}`), `PATCH` restore, `DELETE` | `SetCopyrightInfo` |
| move / copy / status | `PUT /uploads/{id}` (move or copy), `PATCH /uploads/{id}` (`status`, `assignee`), `DELETE /uploads/{id}` | — |
| permissions | `PUT /uploads/{id}/permissions`, `GET /uploads/{id}/perm-groups` | `UploadPermGroups` |
| obligations | `GET /obligations`, `POST /obligations/import-csv\|import-json`, `GET /obligations/export-csv\|export-json` | — |

## The Black Duck → FOSSology flow

The real working pattern this platform sits between: every library of a Black Duck project version
is uploaded to a FOSSology folder, cleared there, and the results are pushed back to Black Duck.
Both directions are fully covered by the two specs, and the outbound half is implemented in
`clreview/deploy.py`, reachable as `POST /api/deploy` (dry run by default).

**Outbound (Black Duck BOM → FOSSology folder)**

1. `GET /api/projects/{p}/versions/{v}/components` — the BOM, with `origins[]` per component
   (`externalNamespace`, `externalId`, `packageUrl`).
2. Resolve a source download URL per component, in order:
   **id2url** from the purl / origin id (see below) → component version SBOM field
   `downloadLocation` → `originUrl` → operator-supplied VCS URL → report `no_source_url`.
   One-call alternative for the whole BOM: `POST …/sbom-reports` (SPDX/CycloneDX with
   `downloadLocation` + `purl` enabled in the SBOM template) and read the `download` link.
3. `GET /folders` → find or `POST /folders` the target folder: default name
   `{project}__{version}`, or the custom name given in the deploy request.
4. `POST /uploads` per component with `uploadType=url` and `location.url` from step 2 (or `vcs` /
   `file` fallbacks), `folderId` of that folder, `mainLicense` from the Black Duck declaration, and
   `scanOptions` enabling `nomos` + `copyright_email_author` (+ `scancode` if installed).
5. Idempotency: `GET /uploads?folderId={f}&recursive=true` and compare `Upload.hash.sha1` /
   `uploadname` before re-uploading; `GET /jobs/{id}` and `GET /uploads/{id}/agents` to wait for
   the agents before reading findings. Implemented today as an upload-name check; job polling is
   still the caller's job (see [ROADMAP.md](ROADMAP.md)).

The deployed folder is then a reviewable scan: `list_scans()` exposes each FOSSology folder as
`folder:<id>`, and `fetch_inventory("folder:<id>")` returns one component per upload, named from
the upload name the deployer chose — which is the Black Duck `name-version`, so matching against
the Black Duck side lines up without fuzzy fallback.

### Step 2 in detail — source URLs with `id2url`

Black Duck knows *what* a library is (registry origin id / purl) but usually not *where* the source
archive is. [`dineshr93/id2url`](https://github.com/dineshr93/id2url) is that missing resolver: it
takes a registry coordinate, checks that the package actually exists in the registry, and returns
the download URL. Eleven registries: npm, dart (pub), pypi, maven (Central + Google Maven), crates,
nuget, rubygems, go, packagist, cocoapods, hex.

Contract (from its README):

```bash
id2url convert maven "com.google.guava:guava:31.1-jre"      # → one URL on stdout
id2url convert npm lodash/4.17.21 --format json            # → {name, version, url}
id2url batch packages.txt --format json -o results.json --continue-on-error   # TSV in: "maven<TAB>g:a:v"
id2url list                                                 # registries it supports
```

```python
from id2url import get_download_url          # raises ValueError when the package is not found
name, version, url = get_download_url("npm", "lodash/4.17.21")
```

Build the batch file straight from the BOM — `origins[].packageUrl` (or `GET …/origin-purls`) maps
to an id2url registry and coordinate. In code that mapping is `deploy.coordinate_from_purl` /
`deploy.coordinate_from_origin`, and the subprocess call is `deploy.run_id2url`:

| Black Duck purl | `registry` | coordinate passed to id2url | typical result |
|---|---|---|---|
| `pkg:maven/g/a@v` | `maven` | `g:a:v` | `…-sources.jar` (source jar, exactly what FOSSology wants) |
| `pkg:npm/name@v`, `pkg:npm/@scope/name@v` | `npm` | `name/v`, `@scope/name/v` | `registry.npmjs.org/…tgz` |
| `pkg:pypi/name@v` | `pypi` | `name/v` | `files.pythonhosted.org/…tar.gz` |
| `pkg:cargo/name@v` | `crates` | `name/v` | `static.crates.io/….crate` |
| `pkg:nuget/id@v` | `nuget` | `id/v` | `api.nuget.org/….nupkg` |
| `pkg:gem/name@v` | `rubygems` | `name/v` | `rubygems.org/downloads/….gem` |
| `pkg:golang/mod@vX.Y.Z` | `go` | `mod/vX.Y.Z` | `proxy.golang.org/…@v/vX.Y.Z.zip` |
| `pkg:composer/v/p@v` | `packagist` | `v/p/v` | GitHub zipball |
| `pkg:cocoapods/pod@v` | `cocoapods` | `pod/v` | GitHub `archive/v.tar.gz` |
| `pkg:hex/name@v` | `hex` | `name/v` | `repo.hex.pm/tarballs/….tar` |
| `pkg:pub/name@v` | `dart` | `name/v` | `pub.dev/api/archives/….tar.gz` |

No purl on the component? Black Duck's own origin string is `namespace:externalId`
(`GET …/origin-purls` returns exactly that pair) — map `namespace` to the registry column and pass
`externalId` through unchanged.

Rules that keep this honest:

- **One `id2url batch` per project version**, `--continue-on-error`, JSON out. A package missing
  from its registry is a per-component `no_source_url` row in the deploy report, never an aborted
  deploy.
- **Call it as a subprocess or a separate service, not as an imported dependency.** id2url is
  AGPL-3.0; a process boundary with a JSON contract keeps clreview's own license clean and still
  costs one call per project version.
- **Trust but verify the fetch.** id2url already checks existence, but FOSSology does the download;
  a URL that 404s later is a per-component failure with the URL recorded, not a silent skip.
- **Archive types FOSSology may not unpack** (`.nupkg`, `.crate`, platform `.gem`): prefer
  `uploadType=url` and let FOSSology detect the mimetype; if unpacking fails, download locally and
  re-upload with `uploadType=file` under a normalized name (`pkg-1.0.zip`).

### Target folder

Default name `{project}__{version}` — Black Duck names carry `/`, spaces and dots, so sanitize:
replace `/ \ : * ? " < > |` and control characters with `_`, collapse runs of separators, trim to
FOSSology's name limit. `Spring Sample` + `1.2` → `Spring_Sample__1.2`.

A deploy request may override it (`{"folder": "Q3-audit"}`) and choose the parent
(`{"parent_folder": 12}`; default = the configured FOSSology root folder). Creation is idempotent
by design: `POST /folders` answers **200 with the existing folder** when the name is taken and
**201 with the new id** when it is not, so re-running a deploy reuses the folder. Store the resolved
`folderId` on the deploy record; every `POST /uploads` in that run uses it.

**Inbound (FOSSology findings → Black Duck)** — this is what clreview does today, now with all
three fields writable: license names, license text, copyrights.

**Optional FOSSology → Black Duck extras** (available, not built): concluded licenses back into the
BOM SBOM fields, a BOM comment recording the FOSSology upload id, and `PUT /uploads/{id}` to move
an upload between folders when a Black Duck project version is renamed.

## Gaps that remain real

- **FOSSology cannot create a copyright statement.** The copyright paths only expose
  `GET` on the collection and `GET/PUT/PATCH/DELETE` on an existing `{hash}`
  (FOSSY yaml 2786–2855). A reviewer can edit or restore an agent finding, not add a statement that
  no agent found. Adding one means the FOSSology UI or a bulk-decision import.
- **Black Duck copyrights are per origin, and there is no documented delete.** A component with
  several origins stores statements on one origin; the connector writes to the first and never
  deactivates existing ones, so a value removed in review stays in Black Duck rather than being
  silently destroyed.
- **A BOM component with several licenses needs disambiguation** before a license-text write; the
  connector refuses instead of guessing (see `_pick_license`).
- **`POST /uploads` needs a reachable source.** Black Duck records `downloadLocation` only when
  someone filled the SBOM field. `id2url` closes most of that gap for registry packages (11
  registries, existence-checked), and it is the documented resolver in this design; what is left
  over is source that never came from a registry — private registries behind auth, vendored copies,
  git-tag-only components — which must come from a VCS URL or a local archive supplied by the
  operator.
- **Agent results are asynchronous.** `/uploads/{id}/licenses` answers `412` until the agent has
  run and `503` while unpacking; a deploy step must poll `/jobs/{id}` rather than read immediately.
- **Black Duck license-name writes remain best-effort** (documented for `SUB_PROJECT` components);
  every write is re-read and verified, and a mismatch is recorded as `failed`.
