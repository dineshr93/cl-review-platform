# CI fix — the chunked-body test dies client-side when the server closes early

Push `08b4eae` fixed the chromium startup (py3.10, py3.11, py3.12 all green). One leg, `test (py3.13)`, now fails on a different test:

```
tests/test_e2e.py:372: in test_chunked_body_is_rejected_not_treated_as_empty
    status, body = self.server.raw("POST", f"/api/mappings/{self.mapping_id}/refresh", body=iter([b"{}"]))
tests/test_e2e.py:54: in raw — conn.request(method, path, body=body, headers=headers)
E   BrokenPipeError: [Errno 32] Broken pipe   (while http.client was sending the terminating b'0\r\n\r\n')
```

What is happening: the test streams a chunked body, the server rejects the request and closes the connection before the client has written the last chunk, and `http.client` surfaces that as `BrokenPipeError` instead of letting the test read the response. Locally the timing hides it; on the runner it lands mid-send. The behaviour under test (a chunked body must not be silently treated as an empty `{}`) is fine — the test's observation of it is too narrow.

Fix the test so it observes the contract instead of one timing:
1. A rejected chunked request may end either with a 4xx response **or** with the connection closed before the response — `BrokenPipeError`, `ConnectionResetError` and `http.client.RemoteDisconnected` all mean "the server refused it". Treat all of them as the expected outcome.
2. Keep the assertion that actually matters and make it the centre of the test: the endpoint must not have treated the body as an empty `{}` — the snapshot count is unchanged and no refresh happened. Assert that in both branches, so a server that silently accepted the body still fails the test.
3. Do not weaken it into a no-op: the test must still fail if the server starts returning 2xx for a chunked body or if the refresh runs.

Where: `tests/test_e2e.py` (the `raw` helper at `:54` and `ErrorBodyTests.test_chunked_body_is_rejected_not_treated_as_empty` at `:372`); `clreview/api.py` or `clreview/server.py` only if the server genuinely should drain the body before closing — if you change server behaviour, say why in the envelope.
Done means: `uv run pytest -q --ignore=adws` is green with the coverage gate passing and `ruff check clreview tests` clean; the chunked test passes when the server closes early AND fails if the server accepts the body — demonstrate both by temporarily flipping the expected branch while developing and reporting what you saw; no other test's assertions changed.
Out of scope: the browser smoke test and its chromium launch (already fixed in `08b4eae`), CI workflow files, the other `ErrorBodyTests` cases, and any change to how non-chunked bodies are handled.
