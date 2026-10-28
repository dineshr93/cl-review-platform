# Attack step 10 — tooling and docs tail (E3, E4, E5, E6, F1, F2, F3, F4, F5)

Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`. Each finding below carries its file:line, evidence, proposed fix, cost and risk there — read them and work them in this order. E1 and E2 already landed; do not redo them.

- **E3** Dockerfile is never built in CI.
- **E4** untracked tooling at the repo root, and a confusing second env file (`.env.sample` vs `.env.example`).
- **E5** CI caching and pinning.
- **E6** `.dockerignore` gaps.
- **F1** test counts wrong in three docs.
- **F2** README API table missing shipped endpoints.
- **F3** `SECURITY.md` overstates credential handling.
- **F4** undocumented CLI/env surface.
- **F5** docs that must be re-verified after the fixes that landed in this session.

Where: `.github/workflows/`, `Dockerfile`, `.dockerignore`, `.gitignore`, `.env.example`, `.env.sample`, `README.md`, `SECURITY.md`, `CHANGELOG.md`, `docs/`, `Makefile`, `justfile`
Done means: CI builds the image (or a job proves it builds) and pins/caches as the finding asks; `.dockerignore` no longer ships what E6 names; the two env files are reconciled to one, or the difference is documented in the file itself; every test count, endpoint row and security claim in the docs matches the code as it now stands — verify each against the suite and the routes, do not copy the old numbers; the CLI/env surface the code actually reads is documented. Report which doc claims you checked and what you found.
Out of scope: A, B, C, D findings; E1, E2 (done); CI platform settings outside the workflow files; rewriting docs prose that is already correct.
