# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: the OSS licence compliance reviewer.** One person, at a desk, working a queue of third-party components for a specific product version. They are not the person who ran the scanner and not the person who administers the project-level (PL) tool; they are the one who has to say which of the two is right, and sign it.

They self-declare who they are: the reviewer name is typed into the top-right of the bar and every decision, note and synchronisation is recorded against it. There are no accounts, no passwords, and no roles for reviewers — **open reviewer identity is the product's decision, not a gap to close.** Any future work on identity must preserve "anyone can enter a name and proceed", and may only make the name more trustworthy (validation, uniqueness, attribution), never gated.

**Secondary: whoever operates the instance.** The same person in practice at this size of tool — they add PL/SL connections, create the project mapping, refresh snapshots, and run the synchronisation. Same open identity, more surfaces.

## Product Purpose

Reconcile copyright and licence data between two tools that will never talk to each other. A source-level (SL) scanner produces evidence about what a component actually contains; a project-level (PL) tool holds what the organisation has recorded. Both are wrong in different places. A human compares them per component and per field, decides which value is correct, and only the explicitly approved corrections are written back — one way, SL → PL, never reverse.

Success is a defensible audit trail: every value that reached the PL tool traces to a named reviewer's decision, made against identified evidence, after a dry run. Not "the sync succeeded" but "this person approved this value from this scan on this date, and here is the raw text it came from".

## Positioning

This is the reconciliation layer between tools, not a competitor to either. Black Duck and FOSSology both ship review UIs; neither can truthfully claim that **raw observation, normalized comparison value, and approved value are three separate stored things**, that a refresh of source data never erases a decision, and that **a decision whose source data has since changed is flagged stale and must be re-reviewed before it can be synchronised.**

It is shipped as open source (MIT) for others to install and self-host, which makes the install story part of the position: Python 3.10+ standard library only, no package install, no build step, no database server. A compliance team in a regulated or air-gapped environment can run it in one command.

## Operating Context

The working path is fixed and reviewers do it hundreds of times a session: **Project overview → Review queue → open a component → decide fields → Approve component for synchronisation → Synchronisation history → Dry run → tick "I approve these writes" → Execute synchronisation.**

- Connections are configured with the **name** of an environment variable holding a credential, never the credential itself.
- The instance binds to loopback by default; beyond that it expects a TLS reverse proxy or a bearer token, and it warns loudly when neither is in place.
- One server process per SQLite file; the database is a file on a volume.
- Matching runs on a hierarchy — purl, then name+version, then strong id, then name-only, then a fuzzy suggestion that a human must confirm before it counts as a match.
- Licence text carries provenance: generic metadata text versus text read from an identified source file. Only identified licence-file text may be synchronised.
- Machine suggestions exist as a separate class from human decisions and are stored in a form an LLM could later fill in; no model is required or shipped.

## Capabilities and Constraints

**Connectors implemented:** Black Duck, FOSSology, ScanCode, mock PL/SL for demo and tests. FOSSID is a placeholder, not a shipped connector. Vendor API objects never cross the connector boundary; connectors translate into internal models.

**Synchronisation is gated by design:** preview → dry run → capability validation → write → verify → audit, with per-component reporting, retry, and idempotent re-runs. A write is refused when the target tool cannot accept that field.

**Known and documented limits** (`docs/LIMITATIONS.md`, each checked against the vendor's own reference implementation, not guessed): Black Duck's `component-origin-copyrights` is read-only, so copyright statements cannot be written back to it. Future work must keep stating such limits rather than working around them silently.

**Hard technical constraints, confirmed as binding:**
- Python 3.10+ standard library only. No runtime dependencies, no build step, no npm.
- The web front end is static vanilla JS against a JSON API. No framework, no bundler.
- One-way SL → PL synchronisation. Nothing writes to the scanner.
- Raw observations, normalized comparison values, and approved values stay separate. A snapshot refresh never destroys a decision.
- Credentials are env-var names; tokens are never stored in the database, returned by the API, or logged.

**Undecided, deliberately:** reviewer roles and permissions (open identity is confirmed; per-field or per-mapping authority is not designed), queue assignment and workload distribution, multi-instance or multi-tenant operation, and whether machine suggestions are ever auto-applied. Record these as open; do not invent an answer.

## Brand Commitments

Named **CL Review** (package `cl_review_platform`, CLI `clreview-server`), MIT licensed. Voice is factual and understated: the documentation's standing rule is that nothing is invented — limitations are verified against reference repositories before being written down. No logo, brand asset, or customer claim exists; none may be fabricated.

## Evidence on Hand

- `clreview/fixtures/demo/` — a seeded fixture set with named real-world cases: identical components, a fuzzy match suggestion, unmatched components on both sides, a copyright formatting variant, a licence detected only in SL, generic PL text versus an original `LICENSE` file in SL, and a false-positive licence.
- `docs/ARCHITECTURE.md`, `docs/CONNECTORS.md`, `docs/LIMITATIONS.md`, `docs/ROADMAP.md`, `SECURITY.md`, `CHANGELOG.md`.
- `DESIGN.md` and `.impeccable/design.json` — the incumbent visual system, extracted from the shipped CSS.
- 64 tests including an end-to-end HTTP workflow; CI on GitHub Actions.
- **Absent and not to be invented:** customers, logos, testimonials, benchmarks, pricing, deployment case studies, screenshots of real vendor data, or a working FOSSID connector.

## Product Principles

1. **Evidence outranks inference.** The raw observation is kept, separate and unoverwritten, so any decision can be re-examined later.
2. **A write is a decision, not a sync.** Nothing reaches the PL tool without a named human approval, an explicit confirmation tick, and a dry run that showed what would change.
3. **Staleness re-opens.** If the source data changes after a decision, the decision is no longer evidence; it must be made again.
4. **Zero-dependency install is the feature.** One command, no toolchain, is how this reaches the environments that need it most.
5. **Say what cannot be done.** A verified limitation stated plainly is worth more than a workaround that quietly loses data.

## Accessibility & Inclusion

Reviewers work this queue for hours at a time, including in dim rooms, and many work without a mouse. WCAG 2.2 AA is the required standard in **both** themes — dark is a working theme, not a night mode — with full keyboard operability, visible focus, and a reduced-motion path that keeps state changes legible. Contrast is measured, not eyeballed.
