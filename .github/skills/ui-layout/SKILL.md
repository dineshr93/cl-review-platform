---
name: ui-layout
description: Layout and information architecture for the CL Review web UI (clreview/web). Use when adding, moving, or redesigning a view, panel, table, form, or action in the UI, or when auditing a screen that looks cluttered or unrelated.
---

# UI layout: placing things on the Reconciliation Desk

Three documents split the design work. Each meaning lives in one of them:

- [`PRODUCT.md`](../../../PRODUCT.md): who the reviewer is and the **working path** (overview → queue → component → decide → approve → dry run → confirm → execute).
- [`DESIGN.md`](../../../DESIGN.md): how things look (tokens, type, components, named rules). Every colour, size, radius, and button variant comes from there.
- **This skill**: *where* things go and *why they sit next to each other*. It decides placement, grouping, order, and disclosure. It never adds a token.

The baseline UI broke this layer: admin forms sit on the overview, panels of equal weight are stacked in arbitrary order, actions are far from what they change, and raw machine values (`{"succeeded": 1}`, `dry_run`, ISO timestamps) are shown as-is. Every rule below fixes one of these failures.

## Steps

1. **Name the job.** Write the view's job as one sentence built on one verb from the working path. Look it up in [`VIEWS.md`](VIEWS.md). A new view gets a new entry there first. *Done when* the sentence has exactly one verb. If you need "and", you have two jobs: split the view or move one job to the view that owns it.
2. **Inventory.** List every element the view renders (read the `CL.render*` function, not the screenshot) and tag each one with exactly one **role**: `context`, `attention`, `work`, `reference`, or `admin` (see the anatomy below). *Done when* every element the render function emits has a role, including empty-state and error branches.
3. **Place.** Assign each element to its zone, and to its owning view for `admin` or another view's `work`. Apply the layout grammar. Record moves as "element → zone/view, rule".
4. **Audit.** Run the checklist at the end of this file against the proposed layout. *Done when* every item passes or has a written reason.
5. **Build and verify.** Implement with the shared zone builders listed under "Shared building blocks" in `VIEWS.md` and the existing classes in `clreview/web/style.css`. Then:
   - `uv run pytest -q tests/test_web_smoke.py tests/test_web_assets.py tests/test_reviewer_required.py`, then `make test` and `make lint`.
   - `make demo`, then screenshot the changed view at 1440×900 in light **and** dark, at 880px, and at 390px. Check each screenshot against the checklist, not by eye alone.
   - Update the view's entry in `VIEWS.md` and any rule in `DESIGN.md` whose wording changed. Refresh `docs/screenshots/` when the view is one of the three shown there.

## View anatomy

Every view has the same four zones, top to bottom. A zone with nothing in it is not rendered.

| Zone | Role | Holds | Weight |
|---|---|---|---|
| **Header** | `context` | the `h1`, one line of context (mapping, component identity), the view's single primary action, right-aligned | always present |
| **Attention** | `attention` | warnings, stale approvals, blockers, read-only state, only when non-empty | full width, above the work |
| **Work** | `work` | the thing the job verb acts on: the queue table, the decision panels, the sync gate | the widest, first thing after attention |
| **Reference** | `reference` | history, notes, provenance detail, raw metadata, secondary tables | below work, collapsed (`<details>`) when long |

`admin` elements (connections, mapping creation, tool config) have no zone on a working view. They live in **Settings & health**, and a working view shows at most a one-line link to them ("2 connections · all ok · Manage").

## Layout grammar

**One view, one job.** A view shows only what its job verb needs. Content that serves another job becomes a link or a summary line pointing at the view that owns it. The overview *summarises* sync history and connection health; it does not host the connection form.

**Reading order is the working path.** Top-to-bottom and left-to-right follow the order the reviewer acts. Evidence comes before the decision it informs, the decision before the approval, the dry run before the execute. When two panels could swap, the one acted on first goes first.

**Proximity.** A control sits beside the thing it changes, at that thing's scope:
- *field* actions go in the field's row, last column;
- *component* actions go in the component header;
- *view* actions go in the view header, right-aligned;
- *mapping* actions (refresh snapshots) go beside the mapping selector, never between unrelated nav links.

Helper and gate text sits directly under the control it explains, in the same group. An explanation that applies to a whole panel goes under the panel title, before the content.

**Action order.** Every in-page action group reads left to right: primary or primary-soft (the recommended decision) → safe secondary → ghost amendments → `.act-divider` → `.danger-cluster`. Modal footers stay right-aligned with the primary last, which is the dialog convention in `DESIGN.md`. Buttons in a table cell go inline in one row (`.row-actions`), not stacked under a badge.

**Gates are steps.** A guarded sequence (dry run → confirm → execute) is laid out as numbered steps (`ol.gate`), each step visibly enabling the next. An unreached step is disabled and says which step unlocks it. A disabled action keeps its own variant at reduced opacity: the numbering carries "not yet", and an irreversible write stays `danger` because it is still destructive once enabled.

**Pairs are symmetric.** PL and SL always appear in the same order (PL left or first, SL right or second), with the same fields in the same order on both sides, in a `.split` or a two-column table. A PL/SL comparison is never one flat `.kv` list where the reader has to find where PL ends.

**Same entity, same columns.** A table of jobs, components, or connections uses the same column set and order on every view it appears in. Identity comes first, then state, then evidence or metrics, then time, then actions. Numbers are right-aligned and tabular.

**Weight follows the job.** The work zone gets full width. Side-by-side panels (`.grid2`) are only for peers of equal importance and similar height. Unequal neighbours stack. A dashboard of equal tiles is a smell unless each tile is a **door**: it links to the filtered view where you act on it, and tiles are ordered by urgency (needs a decision → blocked → done), not by pipeline stage.

**Empty is one line.** An empty section collapses to a single muted line inside its parent (`No suggestions recorded.`) or is omitted when its absence means nothing. A full panel with an empty-state box costs a screen of scroll to say nothing.

**Readouts, not dumps.** Values are rendered for a human:
- status enums become words (`dry_run` → "dry run", `approved_for_sync` → "approved for sync");
- JSON summaries become badges or counts ("1 succeeded");
- timestamps show as relative time with the absolute ISO value in `title`;
- ids are shown with their name;
- hashes are truncated, in mono.

The raw value stays one hover or one `<details>` away, because **Evidence outranks inference** (`PRODUCT.md`).

**Disclose the rare.** Optional fields, advanced settings, raw payloads, decision history, and anything used less than once per session go behind `<details>` or a secondary view. A form shows required fields first, grouped by `<fieldset>` (identity → credentials → advanced, collapsed), in a `.field-grid` capped at a readable width, never as one wrapping row of ten inputs.

**Global chrome is fixed.** The top bar holds product identity, the reviewer field, and the theme toggle. The sidebar holds, in order: the **mapping block** (selector, snapshot status, refresh actions) and then navigation. Neither one gains view-specific content.

## Checklist

- [ ] The job sentence has one verb, and every element serves it or links away.
- [ ] Zones are in order: header → attention → work → reference. No empty zone renders.
- [ ] There is one primary action, in the header or at the end of the work zone, and it names its consequence.
- [ ] Every control is within its scope's container (field, component, view, mapping).
- [ ] Action groups follow the action order, and destructive actions sit last behind a divider.
- [ ] PL/SL pairs are symmetric and ordered PL then SL.
- [ ] Repeated entity tables share columns and order with their other appearances.
- [ ] No raw enum, JSON, or bare ISO timestamp is visible without hovering.
- [ ] Empty sections are one line or absent.
- [ ] Optional and rare content is disclosed, and required content is not.
- [ ] Tab order matches reading order. The keyboard path through the work zone needs no backtracking.
- [ ] The layout holds at 880px (one column, same order) and 640px (tables become cards), with no new horizontal scroll.
- [ ] The view still fits with real-world long values (a UUID mapping name, a long folder path): grid/flex children get `min-width: 0` or `minmax(0, 1fr)`, and single-line values truncate with the full text in `title`. Demo data is short and hides this.
- [ ] Every `DESIGN.md` named rule still holds (One Primary, Verdict Hue, Two-Source, Unbound-Destruction).
