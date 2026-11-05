# API notes (Phase 0 research index)

This file was the Phase 0 deliverable of the original brief. The research itself grew past a
scratchpad, so it lives in its final homes instead of being duplicated here. Nothing in this
repo invents an endpoint: every claim below is checked against the two vendor specifications
copied into `apis/`.

## Primary sources (kept in-repo, unmodified)

| File | What it is |
|---|---|
| `apis/blackduck_Blackduck-REST-API-v2026.4.2.md` | Black Duck REST API reference for the target server version |
| `apis/blackduck_all-properties.md`, `apis/blackduck_using-environment-variables.md` | Black Duck property and environment references |
| `apis/fossology_openapi.yaml` | FOSSology OpenAPI specification |

## Verified findings

- `docs/API-CAPABILITIES.md` — the endpoint matrix actually used: Black Duck read surface,
  Black Duck write surface (copyright add per origin, component license text PUT, best-effort
  license declaration PUT), FOSSology read surface (per-agent findings, copyrights, license
  text, folder/upload listing), FOSSology write surface (status PATCH + comment, folder
  create, upload create), and the BOM→folder deploy flow.
- `docs/LIMITATIONS.md` — every gap that is real: what the vendor APIs do not offer, what is
  best-effort, and the graceful fallback or explicit failure the connector uses for each
  (for example the FOSSology `412`/`503` agent-scheduling states and Black Duck's
  non-destructive copyright storage).
- `docs/CONNECTORS.md` — the connector contract that isolates all vendor quirks from the
  review and sync logic.

## Assumption carried from Phase 0

The live servers were not reachable when this was written, so endpoint behaviour is verified
against the vendor specs and the reference clients (`blackducksoftware/hub-rest-api-python`,
`dineshr93/easy_fossy`), not against a running instance. The open items that need a live
instance to confirm are listed at the end of `docs/API-CAPABILITIES.md` ("Gaps that remain
real") and in `docs/ROADMAP.md`.
