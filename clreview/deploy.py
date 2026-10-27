"""Deploy: Black Duck BOM → registry source URLs (id2url) → FOSSology folder.

The outbound half of the loop. Black Duck knows *what* a library is; `id2url` turns that
registry coordinate into an existence-checked download URL; FOSSology receives the URL and runs
its agents. Everything after that is the review workflow, and the reviewed values go back to
Black Duck through `sync.py`.

`id2url` is AGPL-3.0, so it is invoked as a subprocess with a JSON contract and never imported.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from typing import Any, Callable
from urllib.parse import unquote

from .connectors.base import ConnectorError
from .service import Service, now

# purl type -> id2url registry name (docs/API-CAPABILITIES.md, "Step 2 in detail").
PURL_REGISTRIES = {
    "maven": "maven", "npm": "npm", "pypi": "pypi", "cargo": "crates", "nuget": "nuget",
    "gem": "rubygems", "golang": "go", "composer": "packagist", "cocoapods": "cocoapods",
    "hex": "hex", "pub": "dart",
}
# Black Duck origin namespace -> id2url registry name (fallback when a component has no purl).
ORIGIN_REGISTRIES = {
    "maven": "maven", "npm": "npm", "pypi": "pypi", "crates": "crates", "cargo": "crates",
    "nuget": "nuget", "gem": "rubygems", "rubygems": "rubygems", "golang": "go", "go": "go",
    "composer": "packagist", "packagist": "packagist", "cocoapods": "cocoapods", "hex": "hex",
    "pub": "dart", "dart": "dart",
}
DEFAULT_AGENTS = ["nomos", "copyright_email_author"]

STATUS_UPLOADED = "uploaded"
STATUS_ALREADY = "already_present"
STATUS_NO_URL = "no_source_url"
STATUS_UNSUPPORTED = "unsupported_registry"
STATUS_FAILED = "upload_failed"
STATUS_WOULD_UPLOAD = "would_upload"


def coordinate_from_purl(purl: str | None) -> tuple[str, str] | None:
    """`pkg:maven/com.example/lib@1.0` -> ("maven", "com.example:lib:1.0")."""
    if not purl or not purl.startswith("pkg:"):
        return None
    ptype, _, tail = purl[4:].partition("/")
    registry = PURL_REGISTRIES.get(ptype.lower())
    if not registry:
        return None
    tail = tail.split("?")[0].split("#")[0]
    path, _, version = tail.rpartition("@")
    if not version:
        return None
    path = unquote(path).strip("/")
    if not path:
        return None
    parts = path.split("/")
    if registry == "maven":
        if len(parts) < 2:
            return None
        group, name = ".".join(parts[:-1]), parts[-1]
        return registry, f"{group}:{name}:{version}"
    return registry, f"{path}/{version}"


def coordinate_from_origin(origin: str | None, version: str | None = None) -> tuple[str, str] | None:
    """Black Duck `namespace:externalId` -> id2url coordinate, using the component version when the
    origin string does not carry one."""
    if not origin:
        return None
    namespace, _, rest = origin.partition(":")
    registry = ORIGIN_REGISTRIES.get(namespace.lower())
    if not registry or not rest:
        return None
    parts = rest.split(":")
    if registry == "maven":
        if len(parts) >= 3:
            return registry, ":".join(parts[:3])
        if len(parts) == 2 and version:
            return registry, f"{parts[0]}:{parts[1]}:{version}"
        return None
    if len(parts) == 1 and version:
        return registry, f"{parts[0]}/{version}"
    if len(parts) >= 2:
        return registry, "/".join(parts)
    return None


def run_id2url(coords: list[tuple[str, str]], command: str = "id2url", timeout: float = 900.0) -> dict[str, Any]:
    """One `id2url batch` call. Returns {"urls": {"registry\\tcoordinate": url}, "errors": [...]}."""
    if not coords:
        return {"urls": {}, "errors": []}
    if shutil.which(command) is None:
        raise ConnectorError(f"{command!r} not found on PATH; install it with 'uv pip install id2url' "
                             f"or set config.id2url_command")
    payload = "".join(f"{r}\t{c}\n" for r, c in coords)
    with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as handle:
        handle.write(payload)
        path = handle.name
    try:
        proc = subprocess.run([command, "batch", path, "--format", "json", "--continue-on-error"],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ConnectorError(f"id2url batch timed out after {timeout}s") from exc
    finally:
        import os
        os.unlink(path)
    text = (proc.stdout or "").strip()
    if not text:
        raise ConnectorError(f"id2url batch produced no output (exit {proc.returncode}): "
                             f"{(proc.stderr or '').strip()[:300]}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConnectorError(f"id2url batch returned unparsable output: {text[:200]}") from exc
    urls = {f"{r['registry']}\t{r['origin_id']}": r["download_url"] for r in parsed.get("results", [])}
    return {"urls": urls, "errors": parsed.get("errors", [])}


def sanitize_folder_name(name: str) -> str:
    cleaned = "".join("_" if c in '\\/:*?"<>|\r\n\t' else c for c in name)
    return "_".join(cleaned.split()).strip("_ .")


def _error_key(raw: Any) -> str:
    """id2url reports the input line verbatim; normalize it to the registry\tcoordinate key."""
    parts = str(raw or "").strip().split("\t") if "\t" in str(raw or "") else str(raw or "").split(None, 1)
    return f"{parts[0]}\t{parts[1]}" if len(parts) == 2 else ""


def deploy(svc: Service, *, pl_connection_id: int, sl_connection_id: int, project_id: str,
           version_id: str, folder: str | None = None, parent_folder: int | None = None,
           agents: list[str] | None = None, dry_run: bool = True, actor: str = "reviewer",
           resolver: Callable[[list[tuple[str, str]]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Push every library of a Black Duck project version into a FOSSology folder.

    Per-component statuses: `uploaded`, `already_present`, `no_source_url`,
    `unsupported_registry`, `upload_failed`, `would_upload` (dry run). One unresolved component
    never aborts the run.
    """
    pl = svc.connector_for(pl_connection_id)
    sl = svc.connector_for(sl_connection_id)
    for conn, needed, role in ((pl, "source_coordinates", "pl"), (sl, "ensure_folder", "sl")):
        if not hasattr(conn, needed):
            raise ConnectorError(f"the {role} connection cannot deploy (missing {needed}())")
    agents = agents or DEFAULT_AGENTS
    if resolver is None:
        command = getattr(pl, "config", {}).get("id2url_command", "id2url")

        def resolver(coords):  # noqa: E301 - the subprocess boundary, overridable in tests
            return run_id2url(coords, command=command)

    inventory = pl.source_coordinates(project_id, version_id)

    # one row per component, deduplicated source key so the same archive is uploaded once
    rows: list[dict[str, Any]] = []
    wanted: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in inventory:
        found = None
        for origin in item.get("origins") or [{}]:
            found = coordinate_from_purl(origin.get("purl")) or coordinate_from_origin(
                origin.get("origin") or origin.get("external"), item.get("version"))
            if found:
                break
        registry, coordinate = found if found else (None, None)
        row = {"name": item.get("name"), "version": item.get("version"),
               "registry": registry, "coordinate": coordinate, "status": None, "url": None,
               "message": ""}
        rows.append(row)
        if not registry:
            row["status"] = STATUS_UNSUPPORTED
            row["message"] = "no purl or known registry origin on the Black Duck component"
        else:
            wanted.setdefault((registry, coordinate), []).append(row)

    resolved = resolver(list(wanted.keys())) if wanted else {"urls": {}, "errors": []}
    errors_by_key = {}
    for err in resolved.get("errors", []):
        key = _error_key(err.get("input"))
        if key:
            errors_by_key[key] = err.get("error", "not found")

    if folder:
        folder_name = sanitize_folder_name(folder)
    elif hasattr(pl, "version_label"):
        label = pl.version_label(project_id, version_id)
        folder_name = sanitize_folder_name(f"{label['project']}__{label['version']}")
    else:
        folder_name = sanitize_folder_name(f"{project_id}__{version_id}")
    existing_uploads: list[dict[str, Any]] = []
    target: dict[str, Any] = {"name": folder_name, "id": None, "created": False}
    present: set[str] = set()
    if not dry_run:
        parent = parent_folder or sl.config.get("deploy_parent_folder")
        if not parent:
            raise ConnectorError("a FOSSology parent folder is required (request parent_folder or "
                                 "connection config deploy_parent_folder)")
        target = sl.ensure_folder(folder_name, int(parent))
        existing_uploads = sl.list_uploads(int(target["id"]))
        present = {str(u.get("uploadname") or u.get("uploadName") or "") for u in existing_uploads}

    uploaded: set[tuple[str, str]] = set()
    for (registry, coordinate), targets in wanted.items():
        url = resolved["urls"].get(f"{registry}\t{coordinate}")
        error = errors_by_key.get(f"{registry}\t{coordinate}", "")
        for row in targets:
            if not url:
                row["status"] = STATUS_NO_URL
                row["message"] = error or "id2url returned no URL"
                continue
            row["url"] = url
            base = sanitize_folder_name(str(row["name"] or "component"))
            upload_name = f"{base}-{sanitize_folder_name(str(row['version']))}" if row["version"] else base
            if upload_name in present:
                row["status"], row["message"] = STATUS_ALREADY, f"upload {upload_name!r} already in folder"
                continue
            if dry_run:
                row["status"], row["message"] = STATUS_WOULD_UPLOAD, f"would upload {url}"
                continue
            if (registry, coordinate) in uploaded:
                row["status"], row["message"] = STATUS_ALREADY, "same source already uploaded in this run"
                continue
            try:
                result = sl.upload_url(int(target["id"]), url, upload_name, agents=agents)
                row["status"] = STATUS_UPLOADED
                row["message"] = str(result.get("message", "")) or f"upload accepted for {upload_name}"
                row["upload_id"] = result.get("id")
                uploaded.add((registry, coordinate))
            except ConnectorError as exc:
                row["status"] = STATUS_FAILED
                row["message"] = str(exc)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"] or "unknown"] = counts.get(row["status"] or "unknown", 0) + 1
    report = {"dry_run": dry_run, "folder": target, "project": project_id, "version": version_id,
              "agents": agents, "counts": counts, "components": rows, "generated_at": now()}
    svc.db.audit(actor, "deploy.fossology", "connection", str(sl_connection_id),
                 {"project": project_id, "version": version_id, "folder": folder_name,
                  "dry_run": dry_run, "counts": counts}, now())
    return report
