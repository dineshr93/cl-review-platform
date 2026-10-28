# Security policy

## Reporting

Report vulnerabilities privately through GitHub Security Advisories
(`https://github.com/dineshr93/cl-review-platform/security/advisories/new`) or by email to the
maintainer listed on the repository profile. Do not open a public issue for a vulnerability.

Include the affected version, a minimal reproduction, and the impact (credential exposure,
unauthorized write, data corruption, denial of service).

## Supported versions

Security fixes land on the latest release and `main`. Older tags are not patched.

## Design constraints

- Credentials are read from environment variables **named** in connection configuration. A
  connection row stores the variable name (in `auth_env`), never the value, and the API never
  returns a value: `token`, `password`, `username`, `api_key` and `secret` are redacted from every
  connection listing. Connection creation rejects a `config` key that names a credential with
  `connection config must not carry credentials: … name the environment variable in auth_env
  instead`, and a missing secret fails where it is used (for example `Black Duck token missing
  (set the environment variable named in auth_env.token)`) rather than authenticating anonymously.
  The only path by which a value can reach a connector's `config` is a connector constructed
  directly in a test or script; it is not reachable through the API. Both the rejection and the
  redaction are pinned by tests (`tests/test_connectors.py`).
- Logs carry request lines without credentials or tokens; the server never logs a resolved
  credential.
- Synchronization writes require a human-approved component, `confirm=true`, a tested PL connection,
  and an item cap. Unsupported fields are reported, never faked.
- Set `CLREVIEW_TOKEN` whenever the server binds beyond loopback, and put the API behind an
  authenticating reverse proxy for multi-user deployments.
- The server applies CSP, `nosniff`, frame-deny, and no-store headers; request bodies are bounded.
- SQLite is a local single-writer store. Do not share one database file between concurrently
  running servers.
