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
import re
import threading
from collections import deque
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
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
from .base import ConnectorError, NoWriteEndpoints, RefreshCancelled, Reporting, credential, fetch_text
from .http import HttpError, MAX_PAGES, get_text, join, quote, request


class FossologyConnector(Reporting, NoWriteEndpoints):
    tool = "fossology"
    evidence_role = "FOSSology is an evidence source"
    capabilities = Capabilities(
        read_components=True,
        read_copyrights=True,
        read_license_text=True,
        write_copyrights=False,
        write_license_names=False,
        write_license_text=False,
        write_upload_status=True,
        verify_writes=False,
        notes="evidence source; only the upload's review state is written (PATCH status + comment)",
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
        self.timeout = float(cfg.get("timeout", 180))  # per-file license listings are slow on large uploads
        self.retries = int(cfg.get("retries", 2))
        self.workers = int(cfg.get("workers", 8))
        # "summary" (default): upload-level license/copyright summaries, linked to the FOSSology UI.
        # "files": the per-file listing with license-file text evidence (slow on large folders).
        self.evidence = str(cfg.get("evidence", "summary"))
        self.config = dict(cfg)
        self.folders_as_scans = bool(cfg.get("folders_as_scans", True))
        self._license_text_cache: dict[str, str] = {}
        self._item_maps: dict[str, dict[str, int]] = {}
        self._dir_listings: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._top_items: dict[str, int | None] = {}
        # (upload id, upload date) -> summary component; an unchanged upload is not read twice
        self._summary_cache: dict[tuple[str, str], ComponentData] = {}

    def clear_cache(self) -> None:
        self._summary_cache.clear()

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
        self._check_cancel()
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
        try:
            return json.loads(data.decode("utf-8", "replace")), headers
        except json.JSONDecodeError as exc:
            # A 2xx HTML page (proxy, login form) would otherwise escape as a bare JSONDecodeError.
            raise ConnectorError(f"FOSSology returned a body that is not JSON: {str(exc)[:120]}") from exc

    def _get_text(self, url: str) -> str:
        self._check_cancel()
        return fetch_text(get_text, url, self._headers(), self.timeout, self.retries)

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
        for page in range(1, MAX_PAGES + 1):
            sep = "&" if "?" in url else "?"
            data, headers = self._get(f"{url}{sep}limit={limit}", page=page)
            items = data if isinstance(data, list) else (data or {}).get("items", [])
            out.extend(items)
            total = headers.get("X-Total-Pages") or headers.get("x-total-pages")
            if not items or len(items) < limit:
                break
            if total:
                try:
                    pages = int(total)
                except ValueError as exc:
                    raise ConnectorError(f"FOSSology sent a non-numeric X-Total-Pages: {total!r}") from exc
                if page >= pages:
                    break
        else:
            raise ConnectorError(
                f"listing stopped after {MAX_PAGES} pages of {limit} items ({len(out)} collected); "
                "refusing to report a partial inventory"
            )
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
            scans.extend(self.list_folder_scans())
        return scans

    def list_folder_scans(self) -> list[ScanInfo]:
        # a deployed Black Duck project version is one folder of one upload per library;
        # exposing the folder as a scan lets a whole version be reviewed as one mapping.
        # One request, unlike the paginated upload listing.
        return [ScanInfo(
            external_id=f"folder:{folder.get('id')}",
            name=f"{folder.get('name')} (folder)",
            description=folder.get("description"),
        ) for folder in self.list_folders()]

    def fetch_inventory(self, scan_id: str) -> list[ComponentData]:
        if str(scan_id).startswith("folder:"):
            return self._folder_inventory(str(scan_id).split(":", 1)[1])
        return self._upload_inventory(scan_id)

    @staticmethod
    def _stamp(upload: dict[str, Any]) -> str:
        return str(upload.get("uploaddate") or upload.get("uploadDate") or "")

    def refresh_plan(self, scan_id: str, full: bool = False) -> dict[str, Any]:
        """What a refresh of this scan will read, from one listing request; nothing is fetched."""
        if not str(scan_id).startswith("folder:"):
            return {"items": 1, "unit": "upload", "to_read": 1, "cached": 0,
                    "work": "the per-file license listing, directory tree and license-file text of one upload"}
        uploads = self.list_uploads(str(scan_id).split(":", 1)[1])
        cached = 0
        if self.evidence != "files" and not full:
            cached = sum(1 for u in uploads
                         if self._stamp(u) and (str(u.get("id")), self._stamp(u)) in self._summary_cache)
        work = (f"the per-file license listing of every upload ({self.workers} in parallel; slow on large uploads)"
                if self.evidence == "files" else
                f"upload-level license and copyright summaries, a few requests per upload ({self.workers} in parallel)")
        return {"items": len(uploads), "unit": "uploads", "to_read": len(uploads) - cached, "cached": cached,
                "work": work}

    def _folder_inventory(self, folder_id: str) -> list[ComponentData]:
        def one(upload: dict[str, Any]) -> tuple[list[ComponentData], bool]:
            """The upload's components, and whether they were fetched (False: an unchanged cache hit)."""
            upload_id = str(upload.get("id"))
            upload_name = upload.get("uploadname") or upload.get("uploadName") or upload_id
            if self.evidence != "files":
                stamp = self._stamp(upload)
                if stamp and (upload_id, stamp) in self._summary_cache:
                    return [self._summary_cache[(upload_id, stamp)]], False
                summary = self._upload_summary(upload_id, upload_name)
                if stamp:
                    self._summary_cache[(upload_id, stamp)] = summary
                return [summary], True
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
            return components, True

        # Each upload costs a few round trips; they are independent, so read them concurrently.
        # Results keep the listing order; the first failure or a cancel stops the rest.
        uploads = self.list_uploads(folder_id)
        # Worker threads have their own thread-local state, so they get these from the caller.
        cancel = getattr(self._reporting_local(), "cancel", None)
        progress = getattr(self._reporting_local(), "progress", None)
        stats = self._stats()
        done = 0
        lock = threading.Lock()
        self._report(0, len(uploads))

        def step(upload: dict[str, Any]) -> list[ComponentData]:
            nonlocal done
            self._bind_cancel(cancel)  # this worker's own requests check the same event
            self._check_cancel()
            comps, fetched = one(upload)
            with lock:
                done += 1
                if stats is not None and fetched:
                    stats["read"] += 1
                if progress:
                    progress(done, len(uploads))
            return comps

        pool = ThreadPoolExecutor(max_workers=max(1, self.workers))
        futures = [pool.submit(step, u) for u in uploads]
        try:
            pending = set(futures)
            while pending:
                finished, pending = wait(pending, timeout=0.2, return_when=FIRST_EXCEPTION)
                for future in finished:
                    if future.exception() is not None:
                        raise future.exception()
                if cancel is not None and cancel.is_set():
                    raise RefreshCancelled()
            return [c for future in futures for c in future.result()]
        finally:
            # A cancel or a failure returns now. In-flight reads finish in the background and their
            # results are dropped, rather than holding the refresh for up to timeout x retries each.
            pool.shutdown(wait=False, cancel_futures=True)

    def license_files(self, upload_id: str) -> list[dict[str, Any]]:
        """One upload's license files with their text and the licenses found in each, read now.

        The per-file listing is one paged request; each license file's text one more (capped by
        ``max_license_files``). This is the "files" evidence for a single upload, on demand.
        """
        upload = quote(str(upload_id))
        files = self._pages(join(self.base_url, f"/uploads/{upload}/licenses?agent={quote(self.agents)}"))
        found: dict[str, list[str]] = {}
        for f in files:
            path = f.get("filePath") or ""
            if is_license_file(path):
                found.setdefault(path, []).extend(str(x) for x in ((f.get("findings") or {}).get("scanner") or []) if x)
        paths = list(found)[: self.max_license_files]
        item_map = self._item_map(str(upload_id), paths) if paths else {}
        out = []
        for path in paths:
            item_id = item_map.get(path)
            if item_id is None:
                continue
            try:
                body = self._get_text(join(self.base_url, f"/uploads/{upload}/item/{item_id}/view"))
            except ConnectorError:
                continue
            out.append({"file_path": path, "text": body, "content_hash": sha256(body), "licenses": found[path],
                        "provenance": f"fossology upload item content (upload {upload_id}, item {item_id})"})
        return out

    def _ui_url(self, upload_id: str) -> str:
        """Link to the upload in the FOSSology web UI, where file-level detail and clearing live."""
        root = re.sub(r"/api/v\d+/?$", "", self.base_url)
        try:
            top = self._top_item(upload_id)
        except ConnectorError:
            top = None
        if top is None:
            return f"{root}/?mod=showjobs&upload={quote(upload_id)}"
        return f"{root}/?mod=license&upload={quote(upload_id)}&item={top}"

    def describe_target(self, scan_id: str) -> dict[str, str]:
        """Name and web UI link for a mapped scan (``folder:<id>`` or an upload id)."""
        root = re.sub(r"/api/v\d+/?$", "", self.base_url)
        if str(scan_id).startswith("folder:"):
            folder_id = str(scan_id).split(":", 1)[1]
            name = next((f.get("name") for f in self.list_folders() if str(f.get("id")) == folder_id), None)
            return {"label": name or scan_id, "url": f"{root}/?mod=browse&folder={quote(folder_id)}"}
        data, _ = self._get(join(self.base_url, f"/uploads/{quote(str(scan_id))}"))
        name = (data or {}).get("uploadname") or (data or {}).get("uploadName")
        return {"label": name or scan_id, "url": self._ui_url(str(scan_id))}

    def _upload_summary(self, upload_id: str, upload_name: str) -> ComponentData:
        """One component per upload from the upload-level license and copyright summaries.

        Two requests instead of the per-file listing, tree walk and item reads; file-level evidence
        stays in FOSSology, reachable through ``fossology_url``.
        """
        scanned, _ = self._get(join(self.base_url, f"/uploads/{quote(upload_id)}/licenses/scanned"))
        licenses: dict[str, LicenseEntry] = {}
        for item in scanned or []:
            short = item.get("shortname")
            if short and short not in licenses:
                licenses[short] = LicenseEntry(name=short, spdx=item.get("spdxName") or None, source="fossology",
                                               raw={"occurrence": item.get("occurrence"), "unique": item.get("unique")})
        copyrights = [CopyrightEntry(text=text, origin="fossology copyright agent",
                                     source_path=paths[0] if paths else None)
                      for text, paths in self._aggregate_copyrights(upload_id).items()]
        name, version = split_component_dir(upload_name)
        return ComponentData(
            external_id=f"{upload_id}|{upload_name}",
            name=name,
            version=version,
            metadata={"upload_id": upload_id, "upload_name": upload_name,
                      "fossology_url": self._ui_url(upload_id)},
            copyrights=copyrights,
            licenses=list(licenses.values()),
            raw={"evidence": "summary"},
        )

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

    def upload_status(self, upload_id: str) -> str:
        data, _ = self._get(join(self.base_url, f"/uploads/{quote(str(upload_id))}"))
        return str((data or {}).get("status") or "")

    def close_upload(self, upload_id: str, comment: str = "reviewed", status: str = "Closed") -> dict[str, Any]:
        """PATCH /uploads/{id}?status=Closed with a text/plain comment (required for Closed)."""
        url = join(self.base_url, f"/uploads/{quote(str(upload_id))}?status={quote(status)}")
        try:
            code, _, data = request("PATCH", url, headers=self._headers(), raw_body=comment.encode("utf-8"),
                                    timeout=self.timeout, retries=0)
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc
        text = data.decode("utf-8", "replace") if data else ""
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = text
        if code >= 400:
            raise ConnectorError(f"PATCH /uploads/{upload_id} failed with {code}: {str(parsed)[:200]}", status=code)
        return {"status": code, "body": parsed}

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
                item_map = self._item_map(scan_id, license_paths[: self.max_license_files])
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

    def license_info(self, short_names: list[str]) -> dict[str, dict[str, Any]]:
        """Full name (and SPDX id, where this FOSSology has one) of each short name the scans used.

        Only those names: the full catalogue carries every license text and is far larger.
        A name FOSSology does not answer for is left out, not guessed.
        """
        def one(short: str) -> tuple[str, dict[str, Any] | None]:
            try:
                data, _ = self._get(join(self.base_url, f"/license/{quote(short)}"))
            except ConnectorError:
                return short, None
            if not isinstance(data, dict):
                return short, None
            if data.get("text"):
                self._license_text_cache.setdefault(short, data["text"])
            return short, {"full_name": data.get("fullName") or None, "spdx": data.get("spdxId") or None}

        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            return {k: v for k, v in pool.map(one, short_names) if v}

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

    def _item_map(self, scan_id: str, wanted: list[str] | None = None) -> dict[str, int]:
        """Map upload paths to item ids, breadth-first, one request per directory.

        The walk used to be recursive and serial: a tree of D directories cost D round trips,
        one after another, whether or not the caller needed them. Here a whole level is expanded
        before the next, a directory is opened only when it can still hold a `wanted` path, and
        every listing is remembered per scan - so a second call re-traverses what it already has
        and asks the server only for the branches it has never seen.

        Partial results are safe to keep because the cache records *listings*, not a verdict: a
        directory that was never opened is never treated as empty.
        """
        out = self._item_maps.setdefault(scan_id, {})
        listings = self._dir_listings.setdefault(scan_id, {})
        top = self._top_item(scan_id)
        if top is None:
            return out
        frontier: deque[tuple[int, str]] = deque([(top, "")])
        depth = 0
        while frontier and depth <= 40 and len(out) <= 50000:
            nxt: deque[tuple[int, str]] = deque()
            for dir_id, dir_path in frontier:
                if dir_path in listings:
                    children = listings[dir_path]           # already read: walk it, ask nothing
                elif wanted and not any(p.startswith(dir_path) for p in wanted):
                    continue                                # no wanted path lives under here
                else:
                    children = self._tree(scan_id, dir_id)
                    listings[dir_path] = children
                for child in children:
                    fd = child.get("fileDetails") or {}
                    name, cid = fd.get("fileName") or "", fd.get("id")
                    if not name or cid is None:
                        continue
                    if fd.get("isContainer"):
                        nxt.append((int(cid), f"{dir_path}{name}/"))
                    else:
                        out[f"{dir_path}{name}"] = int(cid)
            frontier = nxt
            depth += 1
        return out

    def _top_item(self, scan_id: str) -> int | None:
        """The upload's root item id, read once per scan. A missing root is an error, not an empty tree."""
        if scan_id in self._top_items:
            return self._top_items[scan_id]
        data, _ = self._get(join(self.base_url, f"/uploads/{quote(scan_id)}/topitem"))
        top: Any = None
        if isinstance(data, list) and data:
            top = data[0].get("message") if isinstance(data[0], dict) else data[0]
        elif isinstance(data, dict):
            top = data.get("message")
        top = int(top) if top is not None else None
        self._top_items[scan_id] = top
        return top

    def _tree(self, scan_id: str, item_id: int) -> list[dict[str, Any]]:
        try:
            data, _ = self._get(join(self.base_url, f"/uploads/{quote(scan_id)}/item/{item_id}/tree/view"))
        except ConnectorError:
            return []
        return data or []
