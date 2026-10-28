# CI fix — browser smoke fails to start chromium on some GitHub runners

Push `a62041e` ran CI on four python legs: `package`, `lint`, `docker`, `test (py3.11)`, `test (py3.13)` passed; **`test (py3.10)` and `test (py3.12)` failed** on the browser smoke test.

Evidence from the runner log:
- `Error: chromium did not start on cdp port 44311: [2624:2651:...:ERROR:dbus/bus.cc:405] Failed to connect to the bus: Could not parse server address` — raised at `tests/web_smoke.mjs:82`.
- The suite otherwise passed on the same runner: `1 failed, 272 passed, 75 subtests`, coverage 90.62%.
- Two of four legs passed, so this is runner variance, not a hard break. The dbus line is usually noise; the chrome log tail is all the runner showed.

Current launch (`tests/web_smoke.mjs:29`): `--headless=new --no-sandbox --disable-gpu --no-first-run --user-data-dir=… --remote-debugging-port=… about:blank`, then a poll of `http://127.0.0.1:<port>/json/list` for a `type === 'page'` target, 80 iterations × 250 ms (~20 s), then throw.

Fix it so the smoke test is reliable on a shared runner without weakening what it checks:
1. Add the flags GitHub runners need — at minimum `--disable-dev-shm-usage` (small `/dev/shm` is the usual killer on `ubuntu-latest`), and whatever else the evidence supports.
2. Make the wait robust rather than a fixed budget: accept a CDP endpoint that answers `/json/version` and create the target if no page exists yet, and/or relaunch once before giving up.
3. On failure, print the whole chrome log and the CDP probe results into the test output, so the next failure names the cause instead of the dbus line.

Where: `tests/web_smoke.mjs`, `tests/test_web_smoke.py` if the timeout needs raising
Done means: `uv run pytest tests/test_web_smoke.py -q --ignore=adws` passes locally (node and chromium are installed here; it runs in ~8 s today), the full `uv run pytest -q --ignore=adws` stays green with the coverage gate and `ruff check clreview tests` clean, and the test still runs unskipped — no `skip`, no `xfail`, no reduced assertion count, no swallowed exit code. State in the envelope why each added flag is needed.
Out of scope: touching CI workflow files, the skip condition in `tests/test_web_smoke.py:16`, the 25 CDP assertions themselves, and any python-version-specific branching.
