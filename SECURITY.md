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

- Credentials are read from environment variables named in connection configuration. Tokens are
  never stored in the database, returned by the API, or written to logs. Connection creation
  rejects a `config` value whose key names a credential (`token`, `password`, `username`,
  `api_key`, `secret`); name the environment variable in `auth_env` instead.
- Synchronization writes require a human-approved component, `confirm=true`, a tested PL connection,
  and an item cap. Unsupported fields are reported, never faked.
- Set `CLREVIEW_TOKEN` whenever the server binds beyond loopback, and put the API behind an
  authenticating reverse proxy for multi-user deployments.
- The server applies CSP, `nosniff`, frame-deny, and no-store headers; request bodies are bounded.
- SQLite is a local single-writer store. Do not share one database file between concurrently
  running servers.
