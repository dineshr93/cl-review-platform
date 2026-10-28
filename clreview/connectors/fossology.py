"""FOSSology source-level connector.

Evidence comes from per-file findings (`/uploads/{id}/licenses`) and, for
license files only, the actual file content (`/uploads/{id}/item/{itemId}/view`).
Generic license metadata (`/license/{shortName}`) is labelled as metadata, never
as component-specific text.

Component grouping is a heuristic (documented in docs/CONNECTORS.md): a single
top-level package tree is one component; otherwise each top-level directory is a
component and root files belong to the upload-named component.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from ..domain import (
    Capabilities,
    ComponentData,
    CopyrightEntry,
    LicenseEntry,
    LicenseTextCandidate,
    ScanInfo,
    is_license_file,
    sha256,
    split_component_dir,
)
from .base import ConnectorError, credential
from .http import HttpError, UnsupportedOperation, get_json, get_text, join, quote, request


class FossologyConnector:
    tool = "fossology"
    capabilities = Capabilities(
        read_components=True,
        read_copyrights=True,
        read_license_text=True,
        write_copyrights=False,
        write_license_names=False,
        write_license_text=False,
        verify_writes=False,
        notes="read-only evidence source; clearing decisions are FOSSology UI concerns, not part of this flow",
    )

    def __init__(self, base_url: str = "", config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None):
        cfg = config or {}
        env = auth_env or {}
        self.base_url = (base_url or cfg.get("base_url") or "").rstrip("/")
        self.token = credential(cfg, env, "token")
        user = credential(cfg, env, "username")
        password = credential(cfg, env, "password")
        self.basic = base64.b64encode(f"{user}:{password}".encode()).decode() if (user and password) else None
        self.agents = str(cfg.get("agents", "nomos"))
        self.max_license_files = int(cfg.get("max_license_files", 10))
        self.generic_text_limit = int(cfg.get("generic_text_limit", 3))
        self.timeout = float(cfg.get("timeout", 30))
        self.retries = int(cfg.get("retries", 2))
        self.config = dict(cfg)
        self.folders_as_scans = bool(cfg.get("folders_as_scans", True))
        self._license_text_cache: dict[str, str] = {}
        self._item_maps: dict[str, dict[str, int]] = {}

    def _headers(self, page: int | None = None) -> dict[str, str]:
        if self.token:
            h = {"Authorization": f"Bearer {self.token}"}
        elif self.basic:
            h = {"Authorization": f"Basic {self.basic}"}
        else:
            raise ConnectorError("FOSSology credentials missing (auth_env.token or auth_env.username/password)")
        if page is not None:
            h["page"] = str(page)
        return h

    def _get(self, url: str, page: int | None = None) -> tuple[Any, dict[str, str]]:
        try:
            status, headers, data = request("GET", url, headers=self._headers(page),
                                            timeout=self.timeout, retries=self.retries)
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc
        if status >= 400:
            raise ConnectorError(f"GET {url} failed with {status}", status=status,
                                 unsupported=status == 412)
        if not data:
            return None, headers
        return json.loads(data.decode("utf-8", "replace")), headers

    def _get_text(self, url: str) -> str:
        try:
            return get_text(url, headers=self._headers(), timeout=self.timeout, retries=self.retries)
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc

    def _post(self, path: str, body: Any, extra: dict[str, str] | None = None) -> tuple[int, Any]:
        """POST without retries: an upload POST is not idempotent."""
        try:
            status, _, data = request("POST", join(self.base_url, path),
                                      headers={**self._headers(), **(extra or {})}, body=body,
                                      timeout=self.timeout, retries=0)
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc
        text = data.decode("utf-8", "replace") if data else ""
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = text
        if status >= 400:
            raise ConnectorError(f"POST {path} failed with {status}: {str(parsed)[:200]}", status=status)
        return status, parsed

    def _pages(self, url: str, limit: int = 100) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 201):
            sep = "&" if "?" in url else "?"
            data, headers = self._get(f"{url}{sep}limit={limit}", page=page)
            items = data if isinstance(data, list) else (data or {}).get("items", [])
            out.extend(items)
            total = headers.get("X-Total-Pages") or headers.get("x-total-pages")
            if not items or len(items) < limit:
                break
            if total and page >= int(total):
                break
        return out

    # --- contract
    def test_connection(self) -> dict[str, Any]:
        data, _ = self._get(join(self.base_url, "/uploads?limit=1"), page=1)
        return {"ok": True, "tool": self.tool, "uploads": len(data or [])}

    def list_scans(self) -> list[ScanInfo]:
        items = self._pages(join(self.base_url, "/uploads"))
        scans = [
            ScanInfo(
                external_id=str(u.get("id", "")),
                name=u.get("uploadname") or u.get("uploadName") or str(u.get("id", "")),
                date=u.get("uploaddate") or u.get("uploadDate"),
                description=u.get("description"),
            )
            for u in items
        ]
        if self.folders_as_scans:
            # a deployed Black Duck project version is one folder of one upload per library;
            # exposing the folder as a scan lets a whole version be reviewed as one mapping
            for folder in self.list_folders():
                scans.append(ScanInfo(
                    external_id=f"folder:{folder.get('id')}",
                    name=f"{folder.get('name')} (folder)",
                    description=folder.get("description"),
                ))
        return scans

    def fetch_inventory(self, scan_id: str) -> list[ComponentData]:
        if str(scan_id).startswith("folder:"):
            return self._folder_inventory(str(scan_id).split(":", 1)[1])
        return self._upload_inventory(scan_id)

    def _folder_inventory(self, folder_id: str) -> list[ComponentData]:
        out: list[ComponentData] = []
        for upload in self.list_uploads(folder_id):
            upload_id = str(upload.get("id"))
            upload_name = upload.get("uploadname") or upload.get("uploadName") or upload_id
            components = self._upload_inventory(upload_id)
            name, version = split_component_dir(upload_name)
            if version and len(components) == 1:
                # a deployed folder holds one upload per library and the deployer named that upload
                # after the source component; that beats guessing from the archive's own directories
                components[0].name, components[0].version = name, version
            for component in components:
                component.external_id = f"{upload_id}|{component.external_id}"
                component.metadata["upload_id"] = upload_id
                component.metadata["upload_name"] = upload_name
                out.append(component)
        return out

    # --- deploy operations (Black Duck BOM -> FOSSology folder)
    def list_folders(self) -> list[dict[str, Any]]:
        data, _ = self._get(join(self.base_url, "/folders"))
        return data if isinstance(data, list) else (data or {}).get("items", [])

    def ensure_folder(self, name: str, parent_id: int) -> dict[str, Any]:
        """Find the folder or create it; POST /folders is idempotent but answers with a message,
        so the id is always resolved from the folder listing."""
        found = {f.get("name"): f for f in self.list_folders() if f.get("parent") == parent_id}
        if name in found:
            return {"id": found[name]["id"], "name": name, "created": False}
        self._post("/folders", None, {"parentFolder": str(parent_id), "folderName": name})
        again = {f.get("name"): f for f in self.list_folders() if f.get("parent") == parent_id}
        if name not in again:
            raise ConnectorError(f"FOSSology did not return folder {name!r} under parent {parent_id}")
        return {"id": again[name]["id"], "name": name, "created": True}

    def list_uploads(self, folder_id: int | str) -> list[dict[str, Any]]:
        return self._pages(join(self.base_url, f"/uploads?folderId={quote(str(folder_id))}&recursive=false"))

    def upload_url(self, folder_id: int | str, url: str, name: str,
                   main_license: str | None = None,
                   agents: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"location": {"url": url, "name": name},
                                "scanOptions": {"analysis": {a: True for a in (agents or ["nomos"])}}}
        if main_license:
            body["mainLicense"] = main_license
        status, parsed = self._post("/uploads", body,
                                    {"folderId": str(folder_id), "uploadType": "url"})
        info = parsed if isinstance(parsed, dict) else {}
        return {"status": status, "id": info.get("id"), "message": str(info.get("message", parsed or ""))}

    def _upload_inventory(self, scan_id: str) -> list[ComponentData]:
        upload = quote(scan_id)
        files = self._pages(join(self.base_url, f"/uploads/{upload}/licenses?agent={quote(self.agents)}&copyright=true"))
        upload_name = self._upload_name(scan_id)
        groups = self._group(files, upload_name)
        copyrights_by_component = self._aggregate_copyrights(scan_id) if files else {}

        item_map: dict[str, int] | None = None
        out: list[ComponentData] = []
        for key, group in groups.items():
            paths: list[str] = group["paths"]
            licenses: dict[str, LicenseEntry] = {}
            for path, names in group["licenses"].items():
                for name in names:
                    lic = licenses.setdefault(name, LicenseEntry(name=name, source="fossology"))
                    lic.evidence_paths.append(path)
            copyrights: list[CopyrightEntry] = []
            seen: set[str] = set()
            for path, texts in group["copyrights"].items():
                for text in texts:
                    if text in seen:
                        continue
                    seen.add(text)
                    copyrights.append(CopyrightEntry(text=text, origin="fossology copyright agent", source_path=path))
            for text, paths2 in copyrights_by_component.items():
                if text in seen:
                    continue
                if any(p in paths for p in paths2):
                    seen.add(text)
                    copyrights.append(CopyrightEntry(text=text, origin="fossology copyright agent",
                                                     source_path=paths2[0]))

            texts: list[LicenseTextCandidate] = []
            license_paths = [p for p in paths if is_license_file(p)]
            if license_paths:
                item_map = item_map or self._item_map(scan_id)
                for path in license_paths[: self.max_license_files]:
                    item_id = item_map.get(path)
                    if item_id is None:
                        continue
                    try:
                        body = self._get_text(join(self.base_url, f"/uploads/{upload}/item/{item_id}/view"))
                    except ConnectorError:
                        continue
                    texts.append(
                        LicenseTextCandidate(
                            kind="source_file",
                            text=body,
                            provenance=f"fossology upload item content (upload {scan_id}, item {item_id})",
                            file_name=path.rsplit("/", 1)[-1],
                            file_path=path,
                            content_hash=sha256(body),
                            is_license_file=True,
                        )
                    )
            for name in list(licenses)[: self.generic_text_limit]:
                generic = self._generic_text(name)
                if generic:
                    texts.append(
                        LicenseTextCandidate(
                            kind="generic_metadata",
                            text=generic,
                            provenance=f"fossology license metadata (/license/{name})",
                            file_name=None,
                            file_path=None,
                        )
                    )

            name, version = split_component_dir(group.get("name") or key)
            out.append(
                ComponentData(
                    external_id=key,
                    name=name,
                    version=version,
                    metadata={"upload_id": scan_id, "source_files": paths},
                    copyrights=copyrights,
                    licenses=list(licenses.values()),
                    license_texts=texts,
                    source_files=paths,
                    raw={"group": key, "file_count": len(paths)},
                )
            )
        return out

    def read_component(self, key: str) -> dict[str, Any]:
        raise UnsupportedOperation("FOSSology is an evidence source; the platform never reads PL values from it")

    def update_licenses(self, key: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        raise UnsupportedOperation("FOSSology is an evidence source; synchronization flows SL -> PL only")

    def update_copyrights(self, key: str, notices: list[str]) -> dict[str, Any]:
        raise UnsupportedOperation("FOSSology is an evidence source; synchronization flows SL -> PL only")

    def update_license_text(self, key: str, text: str, file_name: str | None = None) -> dict[str, Any]:
        raise UnsupportedOperation("FOSSology is an evidence source; synchronization flows SL -> PL only")

    # --- evidence helpers
    def _upload_name(self, scan_id: str) -> str:
        try:
            data, _ = self._get(join(self.base_url, f"/uploads/{quote(scan_id)}/summary"))
            return (data or {}).get("uploadName") or f"upload-{scan_id}"
        except ConnectorError:
            return f"upload-{scan_id}"

    def _group(self, files: list[dict[str, Any]], upload_name: str) -> dict[str, dict[str, Any]]:
        paths = [f.get("filePath") or "" for f in files]
        tops = {p.split("/", 1)[0] for p in paths if "/" in p}
        root_files = [p for p in paths if "/" not in p]
        single = len(tops) <= 1 and (root_files or len(tops) == 1)
        single_key = next(iter(tops)) if tops else upload_name
        groups: dict[str, dict[str, Any]] = {}
        for f in files:
            path = f.get("filePath") or ""
            if single and tops:
                key = single_key
                display = key
            elif "/" in path:
                key = path.split("/", 1)[0]
                display = key
            else:
                key = upload_name
                display = upload_name
            g = groups.setdefault(key, {"name": display, "paths": [], "licenses": {}, "copyrights": {}})
            g["paths"].append(path)
            findings = f.get("findings") or {}
            scanner = findings.get("scanner") or []
            if scanner:
                g["licenses"].setdefault(path, []).extend([str(s) for s in scanner if s])
            for text in findings.get("copyright") or []:
                if text:
                    g["copyrights"].setdefault(path, []).append(str(text))
        return groups

    def _aggregate_copyrights(self, scan_id: str) -> dict[str, list[str]]:
        try:
            data, _ = self._get(join(self.base_url, f"/uploads/{quote(scan_id)}/copyrights"))
        except ConnectorError:
            return {}
        out: dict[str, list[str]] = {}
        for item in data or []:
            text = item.get("copyright")
            if not text:
                continue
            paths = item.get("filePath") or []
            if isinstance(paths, str):
                paths = [paths]
            out[str(text)] = list(paths)
        return out

    def _generic_text(self, short_name: str) -> str | None:
        if short_name in self._license_text_cache:
            return self._license_text_cache[short_name] or None
        try:
            data, _ = self._get(join(self.base_url, f"/license/{quote(short_name)}"))
            text = (data or {}).get("text") if isinstance(data, dict) else None
            self._license_text_cache[short_name] = text or ""
        except ConnectorError:
            self._license_text_cache[short_name] = ""
        return self._license_text_cache[short_name] or None

    def _item_map(self, scan_id: str) -> dict[str, int]:
        if scan_id in self._item_maps:
            return self._item_maps[scan_id]
        data, _ = self._get(join(self.base_url, f"/uploads/{quote(scan_id)}/topitem"))
        top = None
        if isinstance(data, list) and data:
            top = data[0].get("message") if isinstance(data[0], dict) else data[0]
        elif isinstance(data, dict):
            top = data.get("message")
        out: dict[str, int] = {}
        if top:
            self._walk(scan_id, int(top), "", out)
        self._item_maps[scan_id] = out
        return out

    def _walk(self, scan_id: str, item_id: int, prefix: str, out: dict[str, int], depth: int = 0) -> None:
        if depth > 40 or len(out) > 50000:
            return
        try:
            data, _ = self._get(join(self.base_url, f"/uploads/{quote(scan_id)}/item/{item_id}/tree/view"))
        except ConnectorError:
            return
        for child in data or []:
            fd = child.get("fileDetails") or {}
            name = fd.get("fileName") or ""
            cid = fd.get("id")
            if cid is None:
                continue
            if fd.get("isContainer"):
                self._walk(scan_id, int(cid), f"{prefix}{name}/", out, depth + 1)
            else:
                out[f"{prefix}{name}"] = int(cid)
