# View blueprints

One entry per view: the **job** (step 1 of the skill), the zone layout as built, and any defects still open. Strike a defect from the list when it is fixed. Add a new view here before building it.

Render functions: `CL.renderOverview`, `CL.renderQueue`, `CL.renderHistory`, `CL.renderDeploy` in `clreview/web/app.js`; `CL.renderWorkspace` in `clreview/web/workspace.js`; `CL.renderSettings` in `clreview/web/settings.js`.

## Shared building blocks

Build each zone with these, so every view puts the same thing in the same place:

- **Header:** `CL.viewHead(title, ctx, actions)` renders `.view-head`: the `h1` (focus target, `tabindex="-1"`), one `.view-ctx` line, and the view's actions right-aligned. `CL.mappingCtx(m)` is the standard "PL project ↔ SL scan" line.
- **Attention:** `CL.attention(items)` renders the "Needs attention" panel only when `items` is non-empty. Each item says what is wrong and where to fix it.
- **Gates:** `<ol class="gate">` with one `<li>` per step and a `.step-hint` under each control. `li.is-done` turns the number into a check. Steps stay disabled until the previous one is done, and the hint says which step unlocks them.
- **Forms:** `<fieldset>` + `<legend>` groups, `.field-grid` (`.one`, `.four`), labels with a `<span>` wrapper when they carry a muted qualifier. Rare fields go in `<details>`, and the submit goes in `.form-actions`.
- **Readouts:** `CL.words` turns an enum into words; `CL.status` renders a status badge as words with the raw value in `title`; `CL.when` renders relative time with the ISO value in `title`; `CL.jobResult` turns a summary of counts into "1 succeeded". `CL.jobsTable` is the one jobs table.
- **Reference:** `<details class="panel">` for content used less than once per session.

## Global chrome (`index.html`)

- **Top bar:** product name · reviewer field · theme toggle.
- **Sidebar:** `.map-block` (Active mapping select → PL/SL status lines → Refresh snapshots, then `#refresh-progress` while a refresh runs (side, done/total meter, elapsed, time left), then ghost Full refresh / danger-ghost Cancel refresh), then the nav links. Refresh asks `/refresh-plan` first. A long or un-estimable refresh opens a dialog listing per-side work and the estimate, and starts only on approval. Cancel shows "Cancelling…" at once and is disabled during the save phase.

**Mapping context line** (`CL.mappingCtx`): "PL project version ↔ SL scan" as `.map-target` spans, rendered as ids and upgraded in place to tool names with links by `CL.resolveMappingTargets(m, fresh, viewEl)`, scoped to the view (hidden views stay in the DOM). One source for the overview, queue and workspace headers.

## Project overview

**Job:** *see* where this mapping stands and which queue slice to open next.

| Zone | Content |
|---|---|
| Header | mapping name; the mapping context line; primary `a.btn.primary` **Open review queue** (worst-first). No mapping: the primary becomes "Set up connections and a mapping" → Settings. |
| Attention | stale approvals > 0, retrieval `error`/`never`, mapping connection untested or failed. |
| Work | stat tiles ordered by urgency: pending review → stale approvals → match suggestions → unmatched → approved for sync → matched. A tile is a **door** only when the queue can show exactly that set (suggestions, unmatched, approved). Zero stays neutral. |
| Reference | **Sources** (PL / SL `.split`, the same fields per side, then a link to Settings); **Recent synchronization** (`CL.jobsTable` + "All jobs"); **Version delta**. |

Open: the pending-review and stale tiles need a queue filter that accepts several values (a queue/API change).

## Review queue

**Job:** *pick* the next component that needs a decision.

| Zone | Content |
|---|---|
| Header | "Review queue" + count; the mapping context line; view actions: Link components… · divider · ghost Export CSV / Export HTML. No primary: opening a row is the action. |
| Work (selection) | a pick column (header box = this page) and, while anything is picked, a `.bulk-bar` after the table, sticky to the viewport bottom (never above the table, where it would cover the header box and the rows being picked; `#queue-list` is `overflow: visible` and only `.table-scroll` scrolls sideways, so the bar sticks to the page): count · "Select all N matching" (the filtered set, any page) · Clear · primary-soft Approve for sync · Confirm matches · ghost Export selection · divider · danger-ghost Unlink… (confirmed). Selection resets when the filters change; refusals come back in a dialog listing each reason. |
| Work (rules) | `<details>` "Rules" under the filters, counted only when opened: each rule, what it would decide (rows, components, sample) and Apply to N… (confirmed). |
| Attention | none (no mapping: the header primary links to Settings) |
| Work | `.filters` (search, review status, match status, confidence, sort, Search, Clear filters). Option labels are words, and their values stay wire values. → worst-first table (identity · match status words + method · `.conf-meter` · licenses · discrepancies · review status words · match actions) → `.pager`. |
| Reference | none; the shortcuts sheet is on `?` |

## Component workspace

**Job:** *decide* every field of this component, then approve it for synchronisation.

The two verbs are one job because approving is the last decision and only becomes available once every field is decided.

| Zone | Content |
|---|---|
| Header | `h1` "PL name version ↔ SL name version"; context: the mapping context line (its own line, the component's mapping), then match status, method · confidence, review status, discrepancies · decided/total, FOSSology upload link. Actions: primary-soft Confirm match (when unconfirmed) · Edit match… · Highlight diff (`#diff-toggle`; the sticky bar carries a twin `.diff-toggle`) · divider · danger-ghost Unlink. |
| Attention | warnings and stale approvals, one line each; then **Manual match candidates** when present (the match comes before the fields that depend on it). |
| Work | Copyright and license tables carry a pick column (header box = rows still needing a decision) and a `.bulk-bar` with Approve SL · Keep PL · divider · False positive (licenses) · Reject. License text is one `details.lt-row` per BD license (summary: license · what PL holds · recommendation · decision), open by default when there is one; above the rows: Load license files from the SL tool, the FOSSology link and when files were loaded. Each row's actions add Write text… (pasted text with a required source). The approval step offers "Approve as reviewed" when both sides agree. Copyright notices → License names → License text. Rows read PL · SL · class · decision · actions. Open machine suggestions render inline under the row's actions. License text: recommendation, PL/SL texts side by side, decision actions (primary-soft Approve preferred text · Keep PL text · divider · danger-ghost Mark irrelevant) with the sync rule directly under them, and the candidate provenance table in `<details>`. The zone ends with **Synchronization for this component**: the preview table, then the gate ① **Approve component for synchronization** (the view's primary; disabled with "x/y decided" until every field is decided; Withdraw once approved) ② Preview changes / Dry run ③ Apply… (danger). |
| Reference | `<details>` Match evidence and identity (PL / SL `.split`: component, tool, purl, snapshot; then rationale and mismatch); Notes, with decision history in `<details>`; `<details>` Other machine suggestions (decided ones, or ones whose entry left the comparison), only when non-empty. |

## Synchronization history

**Job:** *execute* approved writes to PL behind a dry run, and audit past jobs.

| Zone | Content |
|---|---|
| Header | `h1`; "N components approved for synchronization → PL connection · project version"; no header primary. |
| Attention | read-only mode |
| Work | **Pending synchronization**: one row per component field (component · field · status · note), then the gate ① Dry run, with the last dry run inline (link, result, when) ② "I approve these writes" ③ **Execute synchronization**. |
| Reference | **Jobs**: `CL.jobsTable`; job detail opens under it with readouts, per-component `h3` sections, primary-soft Retry, and Preview undo · divider · danger Undo…. |

Execute stays the `danger` variant, enabled or not: it is an irreversible write to the PL tool, and the smoke test asserts it (`execute is destructive and disabled with a reason`). The gate's numbering carries the "not yet" message.

## Source deployment

**Job:** *deploy* source to the SL tool for a chosen target.

| Zone | Content |
|---|---|
| Header | `h1`; one line on what deployment does. Without a deployable PL + SL pair, the primary links to Settings. |
| Attention | the stale-preview state is carried by the gate (step 1 loses its check, and step 3's hint says to preview again). |
| Work | **Deployment target**: `.split` of fieldsets "From: project-level tool" (tool, project, version) and "To: source-level scanner" (tool, parent folder, folder name, agents) → gate ① Preview sources ② "I approve these uploads" ③ **Upload to scanner** (primary). |
| Reference | `#dp-report`: the preview/deployment report, worst-first, with primary-soft "Create mapping from this folder" after a live run. |

## Settings & health

**Job:** *configure* connections and mappings, and check service health.

| Zone | Content |
|---|---|
| Header | `h1`; "Connections, mappings, license map and service health". No solid primary: each form's submit is primary-soft, the recommended action of its own group. |
| Attention | database not ok, read-only mode, each untested or failed connection. |
| Work | **Connections** (id · name · role · tool · base url · API version · test · permissions as "n of 6" in `<details>` · Test, ghost Edit, divider, danger-ghost Delete) → **Add connection** (fieldsets Identity / Credentials, with the env-var rule under its legend, Advanced in `<details>`, which opens on edit when the connection has advanced values) → **Mappings** (name + active badge · PL · SL · created · danger-ghost Delete…, which lists what will be removed and requires the name typed back) → **New mapping** (PL fieldset / SL fieldset side by side, optional name, "Create mapping and refresh"). A scan or folder list of 12 or more gets a filter box above its select; a shorter one is just the select. |
| Reference | Service health; License map (primary-soft "Suggest entries from PL, SL and SPDX" opens a **Proposals** section: pick column, confident rows pre-ticked, a select where candidates tie, primary Apply n selected · ghost Dismiss); Remembered false positives. |
