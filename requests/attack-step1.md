# Attack step 1 of the scout's order (A1, A2, A3, A4)

Source of truth: `adws/adw_data/sessions/6f231271/context_handoff/scout_findings.md` — each finding below carries its file:line, proposed fix and cost there. Fix them in this order.

- **A1** `clreview/connectors/blackduck.py:229` — the license-name write unpacks `request()`'s 3-tuple into two targets, so it raises `ValueError` after the PUT has already been sent. Use the already-imported `put_json`.
- **A2** `clreview/connectors/fossology.py:311` — component grouping pops from a `set` inside the per-file loop, so the key depends on iteration order when a single top-level dir is present together with root files. Compute the key once before the loop, the way `clreview/connectors/scancode.py:100` does.
- **A3** `clreview/service.py:47` and `clreview/connectors/base.py:43` — credentials are persisted in plaintext, contradicting `SECURITY.md:20`. Make the code and `SECURITY.md` agree; the scout lists both directions in the finding, pick one and say which.
- **A4** malformed input returns 500 where it must return 400.

Where: `clreview/connectors/blackduck.py`, `clreview/connectors/fossology.py`, `clreview/connectors/base.py`, `clreview/service.py`, `clreview/api.py`, `tests/test_connectors.py`, `tests/test_review.py`, `tests/test_suggestions.py`, `SECURITY.md`
Done means: `uv run pytest -q` is green, and each fix is pinned by a test — A1 a connector test asserting the write path returns without raising, A2 a connector test with one top-level dir plus a root file, A3 a test asserting no plaintext credential reaches persisted state, A4 a test asserting 400 on malformed input.
Out of scope: findings A5–A13 and every B/C/D/E/F finding, dependency changes, refactors beyond these four fixes, CI changes.
