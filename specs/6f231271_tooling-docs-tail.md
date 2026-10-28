# Attack step 10 — tooling and docs tail (E3, E4, E5, E6, F1, F2, F3, F4, F5)

Builder: implement in the order below. E6 before E3 (a 220 MB build context makes the new
docker job slow before it makes it useful), tooling before docs (the docs must state the
numbers the new tooling produces). Every number in this plan was measured on this box at
HEAD `d091709`; re-measure and report, do not copy mine blindly.

## Measured baseline (HEAD `d091709`, this box)

| thing | measured |
|---|---|
| `uv run pytest -q --ignore=adws` | **271 passed**, 75 subtests, coverage **91%** (2921 stmts, 274 miss), gate 75 |
| `uv run python -m unittest discover -s tests -p 'test_*.py'` | `Ran 271 tests` — parity holds |
| `uv run ruff check clreview tests` | exit 0 |
| `docker --version` | `Docker version 29.9.2` — docker **is** available locally, so the new job can be proven before pushing |
| build context today (excl. `.venv`, `.git`) | **220 MB** (`du -sh --exclude=.venv --exclude=.git .`) |
| routes in `clreview/api.py` `ROUTES` (lines 37–68) | **31** |
| rows in README API table (lines 209–231) | 23 rows → 27 route patterns → **4 shipped routes undocumented** |
| `git status --porcelain` | only `?? requests/attack-step10.md` — **`justfile`, `.env.sample`, `adws/`, `requests/`, `apis/` are all tracked now** |

Two scout premises are stale and must be reported as such, not "fixed":

- **E4 "untracked tooling"** — `justfile`, `.env.sample`, `adws/` and `requests/` are tracked
  (`git ls-files --error-unmatch` succeeds for each). What is actually left of E4 is the
  `.env.sample` / `.env.example` confusion and one dead recipe.
- **E5 "uv.lock is a 139-byte stub"** — `uv.lock` is 110 KB of real lock since step 2
  (`4c60567`). Nothing to fix; report it.

## A. Tooling

### A1 — E6: `.dockerignore` is 13 lines and lets 220 MB into the build context

`.dockerignore` today (13 lines): `.git`, `.github`, `.venv`, `__pycache__`, `*.pyc`, `*.db`,
`*.db-wal`, `*.db-shm`, `dist`, `build`, `*.egg-info`, `tests`, `docs`.

On disk and **not** ignored: `adws/` **211 MB** (`.gitignore` hides `adws/adw_data/sessions/`
from git, which is exactly the bulk, and `.dockerignore` knows nothing about it), `apis/`
2.8 MB (tracked vendor specs), `.archify/` 1.6 MB, `.impeccable/`, `specs/` 252 KB,
`requests/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage`, `uv.lock`, `justfile`, `.env*`.

Add, grouped with a comment each:

```
# agent/session tooling — never part of the image
adws/
specs/
requests/
.archify/
.impeccable/
# local state and caches
.pytest_cache/
.ruff_cache/
.coverage
.env
.env.*
# not needed to build the wheel
uv.lock
justfile
apis/
```

**Hard constraint:** `Dockerfile:17-18` does `COPY pyproject.toml README.md LICENSE ./` and
`COPY clreview ./clreview`. Do not add `*.md`, `README*`, or anything that matches those four
paths — a `.dockerignore` that ignores `README.md` breaks the build with a confusing
`COPY failed`. Verify after editing: `grep -nE '^(README|pyproject|LICENSE|clreview)' .dockerignore`
must print nothing.

Measure the win with the number BuildKit itself prints, not `du`:

```bash
docker build -t cl-review-platform:ci . 2>&1 | grep -i "transferring context"
```

Report MB before and after. Expect ~220 MB → low single-digit MB.

### A2 — E3: nothing in CI ever builds the Dockerfile

`.github/workflows/` holds `ci.yml` (jobs: `lint`, `test` matrix py3.10–3.13, `package`) and
`release.yml` (jobs: `test` the tag, then `release`). `grep -rn "docker" .github/workflows/`
finds nothing: the image has never been built anywhere, and step 2 said so out loud —
`specs/6f231271_ci-honesty.md:361` scoped "E3–E7 (docker build in CI, uv/pip caching,
`.dockerignore`, action SHA pinning)" out. This step is that debt coming due. `Dockerfile` is 28 lines, `FROM python:3.13-slim`, non-root `USER clreview`,
`HEALTHCHECK` hitting `/api/health`, `ENTRYPOINT ["clreview-server"]`.

Add one job to `ci.yml` (build + run + health; **no registry, no push, no login** — there is no
registry configured and `permissions: contents: read` stays):

```yaml
  docker:
    name: docker image builds and serves /api/health
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>  # pin with the other actions, A3
      - name: Build
        run: docker build -t cl-review-platform:ci .
      - name: Run and wait for the healthcheck
        run: |
          set -o pipefail
          docker run -d --name rev -p 127.0.0.1:8765:8765 \
            -e CLREVIEW_DB=/data/clreview.db cl-review-platform:ci --demo
          for i in $(seq 1 40); do
            code=$(curl -s -o /tmp/h.json -w '%{http_code}' http://127.0.0.1:8765/api/health || true)
            [ "$code" = "200" ] && break
            sleep 1
          done
          docker inspect --format '{{.State.Health.Status}}' rev
          test "$code" = "200"
      - name: Container log on failure
        if: failure()
        run: docker logs rev
```

Keep it honest: the step must fail (not warn) if the health endpoint never answers, and the
`if: failure()` log dump is what makes a red run debuggable. Do not add `--publish`/`push` and
do not add a registry login.

Note while you are in there: `Dockerfile:25-26` hardcodes `127.0.0.1:8765` in the `HEALTHCHECK`,
so the healthcheck lies if `CLREVIEW_PORT` is overridden. The CI job above runs on the default
port, so it is not forced by this step — record it as an observation, and only make the
healthcheck read `$CLREVIEW_PORT` if you can prove it locally with a non-default port.

Prove it locally first (docker 29.9.2 is present): build, run `--demo` on a loopback port,
`curl` `/api/health`, then `docker rm -f`. Report the local build time and the health status.

### A3 — E5: CI caching and pinning

1. **Pin every action to an immutable SHA**, keeping the tag in a comment. **Six** refs are in
   use across both files, none pinned today (`grep -n cache .github/workflows/*.yml` also finds
   no `cache:` key anywhere):

   | ref | used at |
   |---|---|
   | `actions/checkout@v4` | `ci.yml:21,36,63`, `release.yml:27,44` |
   | `astral-sh/setup-uv@v6` | `ci.yml:22,37`, `release.yml:28` |
   | `browser-actions/setup-chrome@v2` | `ci.yml:43` |
   | `actions/setup-python@v5` | `ci.yml:64`, `release.yml:48` |
   | `actions/attest-build-provenance@v2` | `release.yml:71` |

   Get the real SHAs — do **not** copy SHAs from memory or from this plan:

   ```bash
   for a in actions/checkout astral-sh/setup-uv browser-actions/setup-chrome \
            actions/setup-python actions/attest-build-provenance; do
     printf '%s ' "$a"
     git ls-remote "https://github.com/$a" "$(git ls-remote --tags "https://github.com/$a" \
       | awk '{print $2}' | grep -oE 'refs/tags/v[0-9]+$' | sort -V | tail -1)'^{}' \
       | awk '{print $1}'
   done
   ```

   Then `grep -n "uses:" .github/workflows/*.yml` must show a 40-hex ref on every line.
2. **One toolchain in CI.** Two jobs still build the distribution with
   `actions/setup-python@v5` + `python -m pip install --upgrade pip build && python -m build`
   (floating `pip` and `build` versions, no `cache:` key): `ci.yml:64-68` (`package`) and
   `release.yml:48-54` (`release`). Switch **both** to `astral-sh/setup-uv@v6` (which already
   caches uv) + `uv build`. Keep every existing assertion after them: wheel-content checks,
   `clreview-server --version`, the `importlib.resources` asset asserts, the
   `(cd dist && sha256sum * > SHA256SUMS.txt)` / `sha256sum -c` pair (`release.yml:52-57`), the
   `actions/attest-build-provenance@v2` step with `subject-path: dist/*` (`release.yml:69-73`)
   and the `gh release` block. If `uv build` changes artifact names, update the `SHA256SUMS`
   step in the same commit and re-verify with `sha256sum -c`. `release.yml` keeps its
   `contents: write` / `attestations: write` / `id-token: write` permissions; `ci.yml` stays
   `contents: read`.
3. **Base image digest.** `Dockerfile:1` is `FROM python:3.13-slim` with no digest. Pin it:
   `docker buildx imagetools inspect python:3.13-slim --format '{{json .Manifest.Digest}}'`
   (or `docker inspect python:3.13-slim --format '{{index .RepoDigests 0}}'` after a pull) and
   write `FROM python:3.13-slim@sha256:<digest>`. Record the refresh command in a comment above
   the `FROM` line — a digest pin with no refresh note is a slow rot, not a fix. If the box
   cannot reach the registry, keep the tag and say so in the report instead of guessing a digest.
4. Report `uv.lock` as already fixed (110 KB real lock).

### A4 — E4: reconcile the root tooling that is actually broken

- **`.env.sample` vs `.env.example`.** Both are tracked. `.env.example` (16 lines) is the app:
  `BLACKDUCK_URL/TOKEN`, `FOSSOLOGY_URL/TOKEN/USER/PASS`, `SCANCODE_JSON`, `CLREVIEW_TOKEN`.
  `.env.sample` (28 lines) is SSSF agent-roster keys (`OPENROUTER_API_KEY`, `FIREWORKS_API_KEY`,
  `OPENAI_API_KEY`, `PI_PATH`, …) and has nothing to do with the product. Do not merge them and
  do not rename the app one — add a one-line header to each naming the other ("app credentials;
  the SSSF agent-roster keys live in `.env.sample`"), and say which is which in the README
  section A6 adds. Renaming `.env.sample` would break the sssf skill's documented copy step.
- **`justfile:88-89 `obs`** runs `cd .claude/skills/sssf/apps/visualizer && bun install …`;
  that path does not exist in this repo (`ls` → No such file or directory), so the recipe can
  only ever fail. **Delete the recipe** (deletion before addition). Confirm nothing references
  it: `grep -rn "obs" justfile Makefile README.md docs/`.
- Report the tracked/untracked truth: E4's premise is stale; the files are tracked.

## B. Docs

Every fix below is a claim corrected to a measured value. Where a claim is already correct,
say so in the report instead of editing prose.

### B1 — F1: five stale test counts in four files

Actual: **271 tests** (pytest and `unittest discover` agree), coverage **91%**.

| file:line | says | action |
|---|---|---|
| `README.md:46` | `# 79 tests` | → 271 |
| `README.md:255` | `79 stdlib tests cover …` | → 271 |
| `PRODUCT.md:66` | `64 tests including an end-to-end HTTP workflow` | → 271 |
| `CHANGELOG.md:32` (Unreleased) | `102 stdlib tests (23 new deploy tests)` | → 271, and the parenthetical is now meaningless |
| `docs/ROADMAP.md:11` | `5 Validation: fixtures, 102 automated tests …` | → 271 |

`CHANGELOG.md:46` (`63 stdlib tests`) sits in a **released** version section — leave released
history alone; it is a record, not a claim about the current tree.

Then stop the drift at the source: add **one** test, `tests/test_docs.py`, that discovers the
suite and asserts the number printed in `README.md` equals the collected count
(`unittest.TestLoader().discover("tests", top_level_dir="tests")` counted recursively). It
costs about a second and it is the only way these five numbers stay true. State in its
docstring that adding a test means updating README — that is the intended friction. Do not make
it assert prose wording; assert the number only.

### B2 — F2: README API table is missing four shipped routes

`ROUTES` (`clreview/api.py:37-68`) serves 31 patterns; the README table (lines 209–231)
documents 27. Missing rows, with their source lines:

- `GET /api/connections/{id}/folders` — `api.py:41` (FOSSology folders as scans, `folder:<id>`)
- `GET /api/mappings/{id}` — `api.py:61` (single mapping)
- `GET /api/sync/jobs` — `api.py:65` (job list; the table only lists `POST /api/sync/jobs` and `GET /api/sync/jobs/{id}`)
- `GET /api/version` — `api.py:55`

Add the four rows in the table's existing style and position (group with their siblings).
Verify by diffing the two lists mechanically, not by eye:

```bash
grep -o '"/api[^"]*"' clreview/api.py | sort -u          # what ships
grep -oE '`/api[^`]*`' README.md | sort -u               # what is documented
```

Report both lists and the residual diff (it should be empty apart from the combined
`confirm`/`reassign`/`reject` row, which is one row for three routes).

### B3 — F3: SECURITY.md credential claim — now true, but word it to match the code

`SECURITY.md:18-21` claims credentials come from env vars named in config, that tokens are
never stored in the DB, returned by the API, or logged, and that connection creation rejects a
`config` key naming a credential. **This became true when A3 landed in step 1** (`7fd997a`);
the scout's "overstates" note predates it. The live nuance is in
`clreview/connectors/base.py:47-58`:

```python
def credential(config, auth_env, key):
    env_name = auth_env.get(key)
    if env_name:
        return os.environ.get(env_name)
    value = config.get(key)          # fallback for connectors constructed directly
    return str(value) if value else None
```

Tighten the bullet to say exactly what is true, with the enforcement anchors in hand:

- `clreview/service.py:14` — `SECRET_KEYS = ("token", "password", "username", "api_key",
  "secret")`, enforced at `service.py:53-58` with the message
  `"connection config must not carry credentials: … name the environment variable in
  auth_env instead"`, and secrets redacted in listings at `service.py:39`.
- A persisted connection therefore stores only the **name** of an environment variable in
  `auth_env`; the `config` fallback in `credential()` exists for connectors constructed
  directly in tests or scripts and is not reachable through the API.
- A missing secret fails loudly where it is used, e.g. `blackduck.py:77`
  `"Black Duck token missing (set the environment variable named in auth_env.token)"`.

Add the sentence that the rejection is pinned by a test (step 1's A3 test) so the claim has an
executable backstop. Do not weaken or delete the existing claim — it is now correct.

### B4 — F4: document the CLI/env surface the code actually reads

Ground truth:

- **CLI** (`clreview/server.py:34-42`): `--db` (default `$CLREVIEW_DB` or `clreview.db`),
  `--host` (`$CLREVIEW_HOST`, `127.0.0.1`), `--port` (`$CLREVIEW_PORT`, `8765`), `--demo`,
  `--fixtures` (packaged fixtures dir), `--log-level` (`$CLREVIEW_LOG_LEVEL`, `info`),
  `--version`. Console script `clreview-server` (pyproject) ≡ `python3 -m clreview.server`.
- **App env** (`server.py:34-53`): `CLREVIEW_DB`, `CLREVIEW_HOST`, `CLREVIEW_PORT`,
  `CLREVIEW_LOG_LEVEL`, `CLREVIEW_TOKEN`.
- **Connector config keys** (defaults in code): blackduck `base_url`, `timeout` (30),
  `retries` (2); fossology `base_url`, `agents` (`nomos`), `max_license_files` (10),
  `generic_text_limit` (3), `timeout`, `retries`, `folders_as_scans` (true); scancode
  `json_path`; mock `fixtures`; deploy `id2url_command` (`id2url`, `deploy.py:151`),
  `deploy_parent_folder` (`deploy.py:212`).
- `.env.example` lists the 7 connector/token vars and **none** of the five `CLREVIEW_*`.
- README documents `--demo`, `--db`, `--port`, `--log-level`, `CLREVIEW_TOKEN` and points at
  `config.example.json` (README:112, `docs/CONNECTORS.md:189`). Undocumented: `--host`,
  `--fixtures`, `--version`, and the four non-token `CLREVIEW_*` vars.

**The trap to document honestly.** The only `os.environ` read outside `server.py` is
`base.py:56`, and it resolves whatever name `auth_env` supplies. So `BLACKDUCK_URL`,
`FOSSOLOGY_URL` and `SCANCODE_JSON` are **never read by name anywhere in `clreview/`** (grep:
zero hits). `BLACKDUCK_URL=` / `FOSSOLOGY_URL=` in `.env.example` look like app config but the
server never looks at them — a tool's URL is the connection's `base_url` field, supplied when
the connection is created, and `SCANCODE_JSON` works only if a connection lists it under
`auth_env.json_path`. Do not document these as "environment variables the server reads". Say
what they are: names a connection's `auth_env` may point at, exported by the operator so the
connector can resolve them. Same for `FOSSOLOGY_USER`/`FOSSOLOGY_PASS` (basic-auth fallback via
`auth_env`). A reader who exports `BLACKDUCK_URL` and wonders why nothing happens is exactly the
failure this finding is about.

**Operational limits nobody documented.** The knobs a reviewer actually hits are constants with
no home in the docs: `MAX_SYNC_ITEMS_DEFAULT = 500` (`sync.py:24`), `MAX_PAGES = 200`
(`http.py:13`, the A6 pagination cap that raises), `MAX_BODY = 1_000_000` (`api.py:21`),
`SNAPSHOTS_PER_SIDE = 5` (`service.py:17`, step 7's retention), `IN_CHUNK = 500`
(`service.py:16`), queue `limit = min(200, …50)` (`api.py:128`) and audit
`min(500, …100)` (`api.py:233`). Add a third small table for these. Note the trap in
`README.md:86` and `:197`, which call the sync item cap a "default (500)" as if it were
configurable per deployment — it is settable only through `SyncService.__init__(max_items=…)`
(`sync.py:123`), with no env var and no connection config key. Say that plainly or drop the word
"default" in favour of "cap".

**`config.example.json` is decorative.** `README.md:112` says "Example JSON configuration lives
in `config.example.json`", but no code reads that file (grep `config.example` across `*.py`,
`*.js`, `Makefile`, `justfile`, CI → zero hits) and its `"mapping"` block
(`config.example.json:44-51`) has no loader. It is an illustration of API request bodies, not
configuration the server consumes. Reword README and `docs/CONNECTORS.md:189` to say exactly
that — "the shapes the `POST /api/connections` and `POST /api/mappings` bodies take" — or a
reader will export it and wait.

Two more gaps worth closing while the file is open: `--host`, `--port`, `--fixtures` and
`--log-level` have **no argparse help** (`server.py:36-41`), so README is their only
documentation; `--log-level` accepts `debug|info|warning|error`; `--fixtures` defaults to the
packaged `clreview/fixtures/demo`; and `server.py:53-54` logs
`"binding to %s without CLREVIEW_TOKEN: the API is unauthenticated"` for any non-loopback bind
without a token — that warning is the documented reason to set `CLREVIEW_TOKEN`, so cite it.

Do:

1. Add a **Configuration** section to README with three small tables — CLI flags (flag, env
   fallback, default, what it does), the five `CLREVIEW_*` variables the server actually reads,
   and the `auth_env` names a connection may point at (with the "these are names, not app
   config" wording above).
2. Add the four missing `CLREVIEW_*` lines to `.env.example` with a comment each (the Dockerfile
   already sets `CLREVIEW_DB/HOST/PORT`, so the file should not look like they don't exist), and
   retitle its two groups so it is obvious which block is *server config* and which is *names
   `auth_env` resolves*.
3. In `docs/CONNECTORS.md`, add the connector `config` key table with defaults, keyed to the
   code lines above, and keep the pointer to `config.example.json`.
4. Fix the `.env.sample` / `.env.example` cross-reference from A4 in the same section.

Done-criterion check: every name in `grep -rn "os.environ" clreview/` and every
`add_argument` in `server.py` appears in README, **and** README nowhere claims the server reads
`BLACKDUCK_URL` / `FOSSOLOGY_URL` / `SCANCODE_JSON` directly:

```bash
grep -rn "os.environ" clreview/ --include=*.py | grep -v pycache   # 6 reads, all documented
grep -n "add_argument" clreview/server.py                          # 7 flags, all documented
grep -rn "BLACKDUCK_URL\|FOSSOLOGY_URL\|SCANCODE_JSON" README.md docs/  # only as auth_env names
```

### B5 — F5: re-verify the docs against this session's code

The scout snapshot predates nine steps of fixes. Check each claim and edit only what is wrong:

| claim | file:line | verdict / action |
|---|---|---|
| FOSSology tree mapping "costs one call per directory and is cached per upload" | `docs/LIMITATIONS.md:59-61` | **wrong since step 7 (B7)** — `_item_map(scan_id, wanted)` (`fossology.py:372`) is breadth-first and stops once it has the paths it asked for (measured 33→12 requests, 23→2 walks). Reword to the real behaviour, including that a walk which stopped early is not a full listing. |
| Black Duck copyright writes "additive, per origin, never destructive" | `docs/LIMITATIONS.md:10` | check against `update_copyrights` (`blackduck.py:262-300`): one `POST` per new notice, existing notices untouched. Correct — confirm, don't rewrite. |
| "Component license text needs an unambiguous license … fails with the candidate list" | `docs/LIMITATIONS.md:23` | check `_pick_license` (`blackduck.py:415-429`) and `update_license_text` (`:301-315`). Correct — confirm. |
| FOSSology "412 means the agent was not scheduled; 503 unpack agent" | `docs/LIMITATIONS.md:55-57` | matches `fossology.py:86-89`; step 9 pinned the 412 message. Correct — confirm. |
| README "Releases" paragraph | `README.md` Releases section | release.yml now also attests build provenance (step 2, `4c60567`); the paragraph omits it. Add one clause. |
| `docs/ARCHITECTURE.md` "Data model" (lines 23-37) | `docs/ARCHITECTURE.md:23-37` | **silent about step 3** — `db.py` now defines 9 indexes (`:35 :44 :80 :81 :91 :97 :113 :123 :130`) and turns on `PRAGMA foreign_keys=ON` (`db.py:150`, commit `73a63a9`), and the doc's data-model section mentions neither. Add the index list and one line on FK enforcement + cascade behaviour. |
| `db.py:81` comment `-- redundant with matches_pair's prefix; kept for now, drop with B4/B5` | `clreview/db.py:81` | the comment's own trigger (B4/B5) **landed in step 7**, so it now points at finished work. Decide, do not leave the lie: run `EXPLAIN QUERY PLAN` over the match queries; if nothing uses `matches_mapping`, drop that one `CREATE INDEX` and note it in the CHANGELOG, otherwise rewrite the comment to say why it stays. Either way the stale comment must not survive. |
| README matching rules | `README.md:174-178` | five rules listed; `matching.py:139` has a second `name_only` 0.72 path ("name matches; versions agree") the list omits. Add it or fold it into rule 4. |
| `config.example.json` presented as configuration | `README.md:112`, `docs/CONNECTORS.md:189` | **wrong** — nothing loads it (B4). Reword as request-body shapes. |
| sync item cap "default 500" | `README.md:86`, `README.md:197` | value is right (`sync.py:24`) but implies deployment configurability that does not exist (B4). |
| CHANGELOG Unreleased | `CHANGELOG.md:3-…` | steps 1–9 landed since the scout snapshot (indexes + FKs, matching scale, CI honesty, atomicity, read-scale, maintainability, test tail). Add the entries those commits deserve, in the file's existing voice, one bullet per step — and fix the test count (B1). |
| `docs/ROADMAP.md` status rows | `docs/ROADMAP.md:11` | count fix from B1; check no row claims a shipped thing is unshipped. |

Report a checked-list: claim, file:line, correct / corrected, and the code line that decided it.
Claims already verified **correct** at HEAD, so they need no edit and should be reported as
checked rather than rewritten: `docs/API-CAPABILITIES.md:7` (Black Duck spec 42,066 lines —
`wc -l` agrees), `:10` (FOSSology OpenAPI 217 KB / 119 paths), `:11` and `:138` and
`README.md:141-142` and `docs/LIMITATIONS.md:75-76` (11 registries, `deploy.py:24-27`),
`docs/CONNECTORS.md:130` (fossology `agents`/`max_license_files`/`generic_text_limit` defaults,
`fossology.py:56-58`), `docs/CONNECTORS.md:192` (`id2url`, `deploy_parent_folder`),
`docs/CONNECTORS.md:47` and `docs/LIMITATIONS.md:50` (retry statuses 429/500/502/503/504),
`README.md:174-178` confidence values (`matching.py:88,104,121,137,20,158`), the Python 3.10+ /
stdlib-only claims (`README.md:26`, `PRODUCT.md:27,49` vs `pyproject.toml:10`), and
`README.md:53`'s `1.0.0` wheel name (`clreview/__init__.py:2`). No document states a coverage
percentage, so there is no coverage claim to fix.

Context for why F5 is needed at all: the last commit to touch any of
`CHANGELOG.md README.md PRODUCT.md SECURITY.md docs/` was `7fd997a` (step 1); **11 commits**
have landed since (`4c60567 73a63a9 b7fae62 f35e01f 2d80668 455515d 2454d7a b46836e e78b205
d743075 d091709`).

## Order of work and commits

1. `Stop the build context dragging 220 MB of session tooling into the image (E6)`
2. `Build the Docker image in CI (E3)`
3. `Pin CI actions to SHAs and put one toolchain in CI (E5)`
4. `Drop the dead obs recipe and cross-reference the two env files (E4)`
5. `Make the docs tell the truth about tests, routes, credentials and config (F1-F5)`

Commit 5 is docs-only; commits 1–4 touch no `clreview/` source.

## Verification — run all of it and report the output

```bash
uv run pytest -q --ignore=adws                      # expect 271 + the one new docs test, gate 75
uv run ruff check clreview tests                    # exit 0
uv run pytest tests/test_web_smoke.py -q --ignore=adws
python3 -c "import yaml,glob;[yaml.safe_load(open(f)) for f in glob.glob('.github/workflows/*.yml')];print('yaml ok')"
grep -n "uses:" .github/workflows/*.yml            # every ref a 40-hex SHA
docker build -t cl-review-platform:ci . 2>&1 | grep -i "transferring context"   # MB before/after
docker run --rm -d --name rev -p 127.0.0.1:8765:8765 cl-review-platform:ci --demo \
  && sleep 3 && curl -sf http://127.0.0.1:8765/api/health && docker rm -f rev
grep -rn "79 tests\|79 stdlib\|64 tests\|102 " README.md PRODUCT.md CHANGELOG.md docs/   # only released history may remain
grep -nE '^(README|pyproject|LICENSE|clreview)' .dockerignore                             # must print nothing
```

Report: context MB before/after, local docker build time and health status, test count,
coverage %, the doc-claims checklist from B5 with correct/corrected, the route diff from B2,
the two stale scout premises (E4 tracked, E5 uv.lock) called out, and the action SHAs pinned.

## Out of scope

- A, B, C, D findings — already landed in steps 1–9.
- E1, E2 — already landed (`4c60567`).
- Publishing an image: no registry is configured, so no `docker push`, no registry login, no
  `contents: write`. CI builds and runs the image; that is the whole ask.
- Dependabot / Renovate or any new automation service.
- Rewriting released CHANGELOG history (`CHANGELOG.md:46` and below stays).
- Any change to `clreview/` behaviour. This step touches `.dockerignore`, `Dockerfile`
  (base-image digest only), `docker-compose.yml` (only if a claim forces it),
  `.github/workflows/*`, `justfile`, `.env.example`, `README.md`, `PRODUCT.md`,
  `SECURITY.md`, `CHANGELOG.md`, `docs/*.md`, and one new test file.
- Raising `--cov-fail-under`; adding dependencies.
- Renaming `.env.sample` (the sssf skill's documented copy step depends on the name).

## Risks / notes

- **`.dockerignore` over-reach breaks the build.** `Dockerfile:17-18` COPYs
  `pyproject.toml`, `README.md`, `LICENSE`, `clreview/`. No `*.md`-style patterns; run the
  grep check above before committing.
- **Digest pinning rots silently** without automation. That is why the refresh command goes in a
  comment next to the `FROM` line. If the digest cannot be fetched for real, keep the tag.
- **The new docs test is a tripwire by design**: adding a test without updating README turns CI
  red. That is the intended cost of F1 having been wrong five times.
- **`uv build` vs `python -m build`** can differ in artifact filenames; the `SHA256SUMS.txt`
  step and the wheel-content assertions must be re-run in the same commit, not assumed.
- **A docker job on a 220 MB context is slow** — that is why A1 lands before A2.
- `docker-compose.yml` binds `127.0.0.1:8765`, is `read_only` with a `/tmp` tmpfs and inherits
  the Dockerfile `HEALTHCHECK`; it has no resource limits. Nothing here forces a change; if the
  report mentions it, keep it as an observation, not an edit.
