---
name: CL Review Console
description: Side-by-side reconciliation of a project list against scanner license data, for reviewers who sit with it for hours.
colors:
  review-blue: "#245bdb"
  review-blue-deep: "#1d4dbd"
  review-blue-solid: "#245bdb"
  review-blue-wash: "#e9effd"
  verdict-ok: "#14613c"
  verdict-ok-wash: "#e2f1e9"
  verdict-warn: "#7d4b00"
  verdict-warn-wash: "#fbf0d9"
  verdict-bad: "#a7271f"
  verdict-bad-wash: "#fceceb"
  ink: "#1b2330"
  ink-secondary: "#4e5969"
  canvas: "#f5f6f8"
  surface: "#ffffff"
  hairline: "#dcdfe5"
  hairline-soft: "#e8ebef"
  code-surface: "#f0f2f5"
  backdrop: "rgba(17, 24, 34, .55)"
  toast-error: "#8f1f19"
  diff-removed: "#a7271f"
  diff-removed-wash: "#fceceb"
  diff-added: "#14613c"
  diff-added-wash: "#e2f1e9"
  meter-track: "#e8ebef"
typography:
  display:
    fontFamily: "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  headline:
    fontFamily: "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "15px"
    fontWeight: 700
    lineHeight: 1.35
  title:
    fontFamily: "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "12px"
    fontWeight: 650
    lineHeight: 1.4
  body:
    fontFamily: "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.4
  data-mono:
    fontFamily: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
rounded:
  sm: "5px"
  md: "8px"
  lg: "10px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.review-blue-solid}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "6px 11px"
    height: "32px"
  button-primary-hover:
    backgroundColor: "{colors.review-blue-deep}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "6px 11px"
    height: "32px"
  button-danger:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.verdict-bad}"
    rounded: "{rounded.sm}"
    padding: "6px 11px"
    height: "32px"
  button-tiny:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "3px 8px"
    height: "26px"
  field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "6px 8px"
    height: "32px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "16px"
  badge:
    backgroundColor: "{colors.code-surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "1px 7px"
  badge-ok:
    backgroundColor: "{colors.verdict-ok-wash}"
    textColor: "{colors.verdict-ok}"
    rounded: "{rounded.pill}"
    padding: "1px 7px"
  modal:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "24px"
    width: "540px"
  toast:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
  nav-active:
    backgroundColor: "{colors.review-blue-wash}"
    textColor: "{colors.review-blue}"
    rounded: "{rounded.sm}"
    padding: "7px 8px"
---

# Design System: CL Review Console

## Overview

**Creative North Star: "The Reconciliation Desk"**

This interface is a desk where two documents about the same component are laid side by side — the project list's declared license data and the scanner's observed data — and a human decides which one is right. Everything on screen exists to make that comparison fast, legible, and attributable. There is no marketing surface, no empty-state charm, and no decoration that is not also a readout.

The system runs dense and small on purpose. A reviewer triages hundreds of components per session, so information density is the primary aesthetic value: 13px body text, 11px labels, tight table rows, and a two-column desk layout that keeps both sources in one glance. Density is bought back with generous separation between panels, quiet panel titles, and a single accent colour that only ever means "this side" or "this is active".

Dark mode is a working theme, not a night mode. Reviewers sit with this console for hours, often in dim rooms, so every token has a dark twin and contrast is measured in both themes rather than inverted by filter.

**Key Characteristics:**
- Two sources, one screen: the scanner side is accented, the project-list side is neutral. Colour carries provenance, never mood.
- Verdict colours (ok / warn / bad) are reserved for verdict-bearing text and badges, and only when the count is non-zero.
- Flat surfaces, hairline borders, two shadows total. Depth is a response to state, not decoration.
- Small type, tabular numerals, no orphaned empty values.
- Motion is feedback only. Nothing moves while the page is at rest.

## Colors

A single blue accent over a cool neutral ramp, with three verdict hues that appear only where a judgement is being reported.

### Primary
- **Review Blue** (#245bdb): the scanner/SL side, the active navigation item, links, focus rings, and the one primary action per view. It is deliberately rare — under a tenth of any screen — because its rarity is what makes "this is the active thing" readable.
- **Review Blue Deep** (#1d4dbd): hover and pressed state of anything Review Blue.
- **Review Blue Wash** (#e9effd): the quiet tint behind an active nav item, a selected table row, and the focus halo. Never a text colour.

### Secondary
- **Verdict Green** (#14613c) on **Verdict Green Wash** (#e2f1e9): approved, matched, applied, non-zero good news.
- **Verdict Amber** (#7d4b00) on **Verdict Amber Wash** (#fbf0d9): needs attention, partial evidence, warning counts.
- **Verdict Red** (#a7271f) on **Verdict Red Wash** (#fceceb): rejected, failed, blocking errors.

### Neutral
- **Ink** (#1b2330): body text, and the toast's fill (the one place ink becomes a surface).
- **Ink Secondary** (#4e5969): labels, panel titles, metadata, the project-list side of a provenance tab.
- **Canvas** (#f5f6f8): page background behind panels.
- **Surface** (#ffffff): panels, controls, modals, top bar.
- **Hairline** (#dcdfe5) and **Hairline Soft** (#e8ebef): structural borders and table rules respectively.
- **Code Surface** (#f0f2f5): code and license-text blocks, hover wash on controls, resting badge fill.
- **Backdrop** (rgba(17, 24, 34, .55)): modal scrim only.

### Diff & Meter
These are **derived** tokens, not new hues. In `style.css` each one is a `var()` alias of an
existing verdict or neutral token, so a theme change moves the diff paint and the verdict paint
together and they can never drift apart.

- **Diff Removed** (`--diff-del` = Verdict Red Wash, #fceceb) with **Diff Removed Ink**
  (`--diff-del-ink` = Verdict Red, #a7271f): a token present on the project-list side that the
  scanner side does not have. Measured 6.21:1 light, 7.73:1 dark.
- **Diff Added** (`--diff-add` = Verdict Green Wash, #e2f1e9) with **Diff Added Ink**
  (`--diff-add-ink` = Verdict Green, #14613c): a token the scanner found that the project list
  does not declare. Measured 6.41:1 light, 8.22:1 dark.
- **Meter Track** (`--meter-track` = Hairline Soft, #e8ebef): the unfilled rail behind any
  progress or confidence meter. Deliberately the same value as a table rule — a meter is
  structure, not a verdict; only the fill carries meaning.

Diff paint is never applied to a whole row or panel. It marks the changed **token** inside an
otherwise unchanged string, so a reviewer sees which word moved rather than which field is red.

### Named Rules
**The Two-Source Rule.** Colour encodes provenance. The scanner side takes Review Blue; the project-list side takes Ink Secondary. A third hue never stands in for either side.

**The Verdict Hue Rule.** Green, amber, and red appear only on text or badges that report a judgement, and a zero count is never painted in a verdict hue. A stat that reads 0 stays neutral.

**The Measured Pair Rule.** Every colour token has a dark twin, and both are held to WCAG AA against their own background. Contrast is measured, not eyeballed; new colour work ships with a measured ratio in both themes.

**The Token-Level Diff Rule.** A diff highlights the smallest unit that actually changed — a whitespace-delimited token, not a row, a cell, or a panel. Removed tokens take Diff Removed, added tokens take Diff Added, and unchanged tokens in the same string stay untouched. Because the paint is per-token, two sides that differ in one word look 95% identical, which is the correct reading.

**The Derived-Token Rule.** Diff and meter colours are `var()` aliases of verdict and neutral tokens, never independent hex values. Adding a literal here is a mistake: it decouples the diff from the theme and reintroduces the drift the alias exists to prevent.

## Typography

**Display Font:** System sans (with `-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`)
**Body Font:** System sans, same stack
**Label/Mono Font:** System mono (`ui-monospace, "SF Mono", Menlo, Consolas, monospace`) for code, hashes, and license text

**Character:** The platform's own sans, unstyled and unshowy — the type should disappear behind the data. Mono is used only for machine strings (hashes, purls, license text), never as a "technical" costume.

### Hierarchy
- **Display** (700, 20px/1.2, -0.015em): one per view, the page title. Balanced wrap.
- **Headline** (700, 15px/1.35): prose headings inside long-form content; 24px above, 8px below.
- **Title** (650, 12px/1.4, Ink Secondary): panel titles. Small and quiet — the panel's content outranks its label.
- **Body** (400, 13px/1.5): all reading text and table cells.
- **Label** (600, 11px/1.4, uppercase, letter-spaced): field labels, table headers, side labels. Never below 11px.
- **Data Mono** (400, 12px/1.45): code, hashes, license text blocks.

All numerals in tables and stats use `font-variant-numeric: tabular-nums` so columns align by digit.

### Named Rules
**The Small-Type Floor.** 11px is the smallest type in the system. If something needs to be smaller, it needs to be a different design.

**The Tabular Rule.** Any numeral that a reviewer compares down a column is tabular.

## Layout

A fixed 252px sidebar beside a fluid main column (`grid-template-columns: 252px minmax(0, 1fr)`), under a 49px top bar. The sidebar is sticky and scrolls independently; main content is padded 16px/24px and never scrolls horizontally — panels take `overflow-x: auto` instead so a wide table scrolls inside its own frame.

Spacing runs on a five-step scale (4 / 8 / 12 / 16 / 24). Panels take 16px padding and 12px bottom margin; more space sits above a heading than below it. Repeating grids are content-driven, not viewport-driven: `.grid2` at `minmax(280px, 1fr)`, `.grid3` and `.stats` at 200px/132px minimums, so columns wrap when the content runs out of room rather than at a device width.

Two content breakpoints, both chosen by where the layout actually breaks:
- **880px** — the desk collapses to one column. Sidebar stacks above the content, nav becomes a wrapping grid, split panels stack, form fields go full width.
- **640px** — data tables become cards. Each row turns into a label/value list via `data-label`, so a phone reads a component without horizontal scrolling. Measured: zero horizontal overflow at 320, 390, and 768px, and at 200% text scaling.

Input method is detected separately from width. Under `pointer: coarse`, controls lift to a 40px minimum height and nav links gain padding, because a thumb is not a mouse at the same resolution.

### Named Rules
**The Card-on-Narrow Rule.** Below 640px a table is a stack of cards. A phone never scrolls sideways to read a component.

**The Content-Driven Grid Rule.** Grids break at their content's minimum readable width (`minmax(280px, 1fr)`), not at device widths.

## Elevation & Depth

The system is flat at rest and lifts only what floats. Two shadows exist in total, and both are theme-specific.

### Shadow Vocabulary
- **Resting** (`box-shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.07)`): panels, stat tiles, primary buttons. A one-pixel whisper that separates a surface from canvas; invisible as a shape, present as an edge.
- **Floating** (`box-shadow: 0 12px 28px rgba(16,24,40,.18), 0 3px 8px rgba(16,24,40,.10)`): modals and toasts — the only things that sit above the page plane.
- **Focus halo** (`box-shadow: 0 0 0 3px var(--accent-weak)`): state ring on inputs and selects; not an elevation.

### Named Rules
**The Flat-By-Default Rule.** Surfaces are flat at rest. Hover changes colour, never height. Nothing gains a shadow because it was hovered.

## Shapes

Corners run on four values and nothing else: 5px for controls (buttons, inputs, selects, nav items), 8px for panels and stat tiles, 10px for overlays, and a full pill for badges. The progression is deliberate — the closer something floats above the page, the softer its corner.

Borders are 1px hairlines; the page has no outer frame, no gradient, and no glass. The one non-hairline border in the system is the 3px provenance tab on license-text and note blocks (`.notice`): scanner side takes Review Blue, project-list side takes Ink Secondary, variant warnings take Amber. It is a data encoding, not an accent, and it is the only place a thick single-side border is allowed.

Rejected decisions are struck through and dimmed to 60% rather than removed — the desk keeps the evidence of the decision.

## Components

### Buttons
- **Shape:** slightly rounded (5px), 32px tall minimum, 6px/11px padding.
- **Primary:** Review Blue fill, white text, 600 weight, resting shadow. Hover deepens to Review Blue Deep. One per view, and it names the action.
- **Secondary:** surface fill, hairline border, ink text. Hover takes the code-surface wash and an accent border.
- **Danger:** surface fill with verdict-red text and a 45%-mixed red border; hover fills the red wash. Destructive actions are named in the button, not coloured louder.
- **Tiny:** 26px tall, 12px text, for in-row table actions.
- **Primary Soft** (`.primary-soft`): the review-row variant of primary — Review Blue text on the Review Blue wash instead of a solid fill. It is used where a view already has a solid primary and a row still needs to point at the recommended decision ("Approve SL"). Same hierarchy job, one shade quieter, so six buttons in a row do not compete with the page's own primary.
- **Ghost** (`.ghost`): transparent fill, Ink Secondary text, for edits that are always available but never the headline ("Correct…", "Note…"). Hover takes the code-surface wash and promotes the text to Ink.
- **Danger Ghost** (`.danger-ghost`): transparent fill, Verdict Red text, **no border at rest**. Destructive actions ("Reject", "False positive") are quieter than the safe ones until the pointer is on them, then they take the red wash and a 40%-mixed red border. A mis-click is cheaper to avoid when the dangerous thing is the least prominent thing on the row.
- **Action grouping:** quiet edits and destructive actions are separated by a 1px `.act-divider` and the destructive pair is wrapped in a `.danger-cluster`, so the eye reads three groups — decide, amend, destroy — instead of six equal buttons.
- **States:** disabled drops to 45% opacity, loses its shadow, and keeps its border. Press scales to 0.97 (80ms) — the only place a button moves. A disabled control always says why: a `title` on hover and a line of helper copy beside it naming the action that enables it ("Execute stays disabled until you tick the approval box").
- **Busy:** a button that starts a long request shows an inline `.btn-spin` ring (11px, currentColor, 0.7s) next to its label while the call is in flight, in addition to the global top-edge progress bar. The control that started the work reports the work.

### Chips / Badges
Pill (999px), 11px text, 1px/7px padding, hairline border. Resting badges take the code surface; verdict badges take their wash, their hue for text, and a 40%-transparent border of the same hue. Badges carry status words, not decoration.

### Panels / Containers
- **Corner style:** 8px. **Background:** surface on canvas. **Border:** 1px hairline. **Internal padding:** 16px, 12px bottom margin, last child loses its bottom margin.
- Panels scroll their own overflow rather than widening the page.

### Inputs / Fields
Surface fill, 1px hairline, 5px radius, 32px minimum height, 13px text. Focus shifts the border to Review Blue and adds a 3px wash halo (no default outline). Placeholders are hints, never the label — every field has a real `<label>` or an `aria-label`. Touch input lifts fields to 40px. A refused field takes the verdict red on border plus a 3px red halo, `aria-invalid="true"`, and an inline error in verdict red at 11px directly under the field, in flow, so the bar grows instead of covering anything; the error names the recovery ("Enter your name here first, then repeat the action.") and clears the moment the field is repaired.

### Navigation
Sidebar links are 7px/8px blocks at body size; hover takes the code-surface wash, active takes the Review Blue wash with accent text at 600 weight. The active mapping is a labelled `<select>` above the links, not a heading. Below 880px the nav becomes a wrapping grid.

### Modal
540px max, 24px padding, 10px radius, floating shadow, scrim at the backdrop token. Focus is trapped, Escape closes, focus returns to the opener. Titles are 16px; the field label sits above the input at 11px. It replaces every native `prompt()`/`confirm()`.

### Toast
Ink fill with surface text, bottom-right, 520px max width, floating shadow, a dismiss button, and `role="status"` (errors announce as `role="alert"`). Errors take the toast-error red with white text — the one place a hard-coded fill survives, because it must stay red in both themes.

### Data Table
12px, 7px/8px cell padding, hairline-soft row rules, header row at 11px uppercase with `scope="col"`. A clickable row takes the accent wash on hover and an inset focus ring. The row's link grows its hit area into the cell padding with a negative margin, so touch targets reach 24px without changing row density.

### Progress
A 2px indeterminate bar pinned to the top edge, visible only while a request is in flight (`body.busy`). At rest it is invisible and its animation is not running.

### Meters
Two determinate meters share one rail token (**Meter Track**) and differ only in what the fill means.

- **Decision progress** (`.ws-meter`, 120×6px): how far through a component the reviewer has got. The fill is Review Blue; the count beside it (`.ws-count`) turns Verdict Green at 100%. Decision points are counted, not guessed: one per copyright row, one per license row, plus one for the license-text field.
- **Confidence** (`.conf-meter`, 46×5px): how sure the matcher is, in the queue. The fill is hue-coded by band — Verdict Green at ≥0.90, Verdict Amber at ≥0.70, Verdict Red below — and the numeric value sits next to it in tabular figures. A bare `0.99` asks the reviewer to remember the bands; a coloured bar plus the number does not.

Both meters are `overflow: hidden` rails with an inner `i` whose width is the value, so a fill never escapes its pill.

### Sticky Context Bar
While reviewing a component, the title scrolls away and the reviewer loses the thread — which library, how sure the match is, how much is left. `.ws-sticky` is a fixed bar that carries exactly those four things (name, match badge, confidence, progress meter) and appears **only after the page `h1` has scrolled out of view**, via an `IntersectionObserver` on the title itself. At the top of the page it is absent, so the title is never doubled or covered.

It is built from `<span>`s, never a second heading — the view keeps exactly one `h1`. It is disabled below 880px, where the layout collapses and the title is close enough to re-scroll anyway. It enters with a 140ms slide-down and costs nothing at rest.

### Diff Paint
Word-level diff marks (`mark.d-del`, `mark.d-add`) inside copyright notices, license names, and the license-text blocks. See **The Token-Level Diff Rule** under Colors for what they mean and how they are derived.

A reviewer who wants the plain text can turn the paint off with the `#diff-toggle` switch (`role="switch"`), which adds `.diff-off` to the view: the marks stay in the DOM and only their colour changes, so toggling costs no re-render and never re-escapes anything. Diffing is capped at 1,500 tokens per pair — past that the LCS cost stops paying for itself and the plain text is shown.

### Keyboard Cursor
In the review queue, the row selected by `j`/`k` takes `.kbd-cursor`: the accent wash plus a 2px inset accent outline, so the cursor is visible even on a row that is already highlighted. It is a visual echo of DOM focus, not a replacement for it — `Enter`/`o` open the focused row.

### Shortcuts Sheet
`?` opens a modal listing every binding, reusing the standard modal and scrim. Destructive actions are shown as deliberately unbound rather than omitted, so the absence reads as a decision and not an oversight.

### Named Rules
**The One Primary Rule.** One primary button per view. Everything else is secondary, tiny, or danger. A review row may use **Primary Soft** — the same pointing job at wash intensity — so the row never out-shouts the view's own primary.

**The Named Action Rule.** A control states its consequence ("Execute synchronization", "Reject"), and a destructive control says so in words before it is pressed.

**The Unbound-Destruction Rule.** Destructive actions have no keyboard shortcut, on purpose. This platform's whole stance is that a human gates a write; a single keystroke that rejects a component would be the one place the UI is faster than the reviewer's intent. Keys cover the safe half of the workflow (`a` approve, `p` keep, `e` correct, `n` note) and the sheet says so.

**The Attributed Write Rule.** Identity is open — anyone types a name and proceeds — but nothing is recorded without one. A write with no reviewer is refused at the field, not silently stamped with a placeholder, and the refusal says where to fix it. The server refuses too (400 with `reviewer_required`), because the browser is not the only client.

**The Worst-First Rule.** The queue opens on the rows that need a decision: most discrepancies, then least certain matches. A count is painted in a verdict hue only when it is above zero, and a filter control that does nothing yet is shown disabled rather than silently inert.

## Do's and Don'ts

### Do:
- **Do** keep the type scale at 13px body / 11px label, and never go below 11px.
- **Do** add a dark twin for every new colour token and measure both against their own background before shipping.
- **Do** reserve Review Blue for scanner-side, active, and focus. If it is not one of those three, it is the wrong colour.
- **Do** use the verdict washes for badges and verdict text only, and leave zero counts neutral.
- **Do** reach for the spacing scale (4/8/12/16/24) via `.mt-2`/`.mt-3` or panel rhythm — never an inline `style` attribute.
- **Do** keep the empty state a single em-dash token so "no data" looks identical everywhere.
- **Do** keep every interactive control at 24px minimum, and expect 40px under `pointer: coarse`.
- **Do** use the modal for any prompt or confirmation; native `prompt()`/`confirm()` are blocked because they are unlabelled, unstyleable, and thread-blocking.
- **Do** gate any looping animation to a real in-flight state.
- **Do** bind keyboard shortcuts only to reversible actions, and list every binding in the `?` sheet — including the destructive ones marked as deliberately unbound, so the omission reads as a decision.

### Don't:
- **Don't** add a coloured left border to a new card, callout, or alert. The 3px provenance tab on license blocks is a data encoding; a fourth meaning for it is not allowed.
- **Don't** use `transition: all` or `will-change`. Both are currently at zero occurrences; keep them there.
- **Don't** animate anything at rest, and don't animate layout properties. Movement is transform and opacity only.
- **Don't** hard-code colours. Three literals remain (`#fff` on filled buttons, `#8f1f19` on the error toast) and they are grandfathered; new work adds none.
- **Don't** use an inline `style` attribute for spacing or colour. The one exception is a meter's `width`, which is a data value being rendered, not a design decision.
- **Don't** reduce motion by killing all motion. Travel goes; colour and opacity feedback stays, so a state change still reads.
- **Don't** paint a stat red or amber when its value is zero.
- **Don't** let a table force the page to scroll horizontally; scroll inside the panel or become cards.
- **Don't** use the `.notice` class for anything but a review note. Its count is asserted in the browser smoke test as the number of notes on a component, so a diff callout or a system banner wearing that class breaks the count. Diff callouts get their own classes.
