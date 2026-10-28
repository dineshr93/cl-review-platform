# Attack step 8 — maintainability (D1, D2, D3, D4, D5)

Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md`. Each finding below carries its file:line, evidence, proposed fix, cost and risk there — read them and work them in this order. D1 first: deleting code is the cheapest diff in the list.

- **D1** Dead code shipped in the package.
- **D2** Duplicated logic that has already drifted.
- **D3** Oversized functions mixing fetch + template + binding.
- **D4** Frontend correctness/maintainability items.
- **D5** Backend/API shape nits.

Where: whatever the findings name — expect `clreview/` modules, `clreview/web/*.js`, `clreview/connectors/`, `pyproject.toml` packaging entries, and the tests that cover each area
Done means: `uv run pytest -q --ignore=adws` is green with the coverage gate passing and `ruff check clreview tests` exits 0; every deletion is justified in the plan by evidence that nothing reaches it (grep/import evidence, not a hunch); every deduplication in D2 names the drifted copies and states which behaviour won and why, with a test pinning the surviving behaviour; the browser smoke test still passes (`uv run pytest tests/test_web_smoke.py -q --ignore=adws`); no public route or response shape changes silently — if D5 forces one, list it in the plan and pin it with a test. Report the new total coverage and the net line delta.
Out of scope: A, B, C, E, F findings; framework migrations; new dependencies; reformatting untouched code; renames that only satisfy taste.
