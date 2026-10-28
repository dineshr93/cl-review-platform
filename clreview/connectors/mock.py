"""Demo / test connectors backed by JSON fixtures.

They implement the same contracts as the real vendors and declare full write
capability, so the review-to-sync workflow can be exercised without credentials.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

from ..domain import Capabilities, ComponentData, CopyrightEntry, LicenseEntry, LicenseTextCandidate, ProjectInfo, ScanInfo, sha256
from .base import ConnectorError


def _load_path(path: str, default_name: str) -> str:
    if not path:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "fixtures", "demo", default_name)
    return path


def _load(path: str, default_name: str) -> dict[str, Any]:
    with open(_load_path(path, default_name), encoding="utf-8") as fh:
        return json.load(fh)


def _component(raw: dict[str, Any]) -> ComponentData:
    return ComponentData(
        external_id=raw.get("external_id") or raw.get("name", "unknown"),
        name=raw.get("name", "unknown"),
        version=raw.get("version"),
        purl=raw.get("purl"),
        metadata=copy.deepcopy(raw.get("metadata", {})),
        copyrights=[CopyrightEntry(text=c["text"], origin=c.get("origin", "mock"), source_path=c.get("source_path"))
                    for c in raw.get("copyrights", [])],
        licenses=[LicenseEntry(name=l["name"], spdx=l.get("spdx"), license_type=l.get("license_type", "declared"),
                               evidence_paths=list(l.get("evidence_paths", [])), source="mock")
                  for l in raw.get("licenses", [])],
        license_texts=[LicenseTextCandidate(kind=t.get("kind", "unknown"), text=t["text"],
                                            provenance=t.get("provenance", "mock"),
                                            file_name=t.get("file_name"), file_path=t.get("file_path"),
                                            content_hash=t.get("content_hash"),
                                            is_license_file=bool(t.get("is_license_file", False)))
                       for t in raw.get("license_texts", [])],
        source_files=list(raw.get("source_files", [])),
        raw=copy.deepcopy(raw),
    )


class MockPLConnector:
    tool = "mock-pl"
    capabilities = Capabilities(
        read_components=True, read_copyrights=True, read_license_text=True,
        write_copyrights=True, write_license_names=True, write_license_text=True,
        verify_writes=True, notes="demo connector: all writes supported",
    )

    def __init__(self, base_url: str = "", config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None):
        cfg = config or {}
        self.config = dict(cfg)
        self._path = cfg.get("fixtures", "")
        data = _load(self._path, "pl.json")
        self._mtime = os.path.getmtime(_load_path(self._path, "pl.json"))
        self.projects: list[dict[str, Any]] = copy.deepcopy(data.get("projects", []))
        self._fail_once: set[str] = set()

    def _maybe_reload(self) -> None:
        """Reload only when the fixture file changed; in-memory writes survive."""
        path = _load_path(self._path, "pl.json")
        mtime = os.path.getmtime(path)
        if mtime != self._mtime:
            self._mtime = mtime
            self.projects = copy.deepcopy(_load(self._path, "pl.json").get("projects", []))

    # --- discovery
    def test_connection(self) -> dict[str, Any]:
        return {"ok": True, "tool": self.tool, "projects": len(self.projects)}

    def list_projects(self) -> list[ProjectInfo]:
        self._maybe_reload()
        return [ProjectInfo(external_id=p["id"], name=p.get("name", p["id"])) for p in self.projects]

    def list_versions(self, project_id: str) -> list[str]:
        self._maybe_reload()
        for p in self.projects:
            if p["id"] == project_id:
                return [v["id"] for v in p.get("versions", [])]
        raise ConnectorError(f"project {project_id!r} not found")

    def _version(self, project_id: str, version_id: str) -> dict[str, Any]:
        for p in self.projects:
            if p["id"] == project_id:
                for v in p.get("versions", []):
                    if v["id"] == version_id:
                        return v
        raise ConnectorError(f"project version {project_id}/{version_id} not found")

    def _find(self, key: str) -> dict[str, Any]:
        try:
            project_id, version_id, external_id = key.split("|", 2)
        except ValueError:
            raise ConnectorError(f"bad component key {key!r}") from None
        for c in self._version(project_id, version_id).get("components", []):
            if (c.get("external_id") or c.get("name")) == external_id:
                return c
        raise ConnectorError(f"component {key!r} not found")

    def fetch_inventory(self, project_id: str, version_id: str) -> list[ComponentData]:
        self._maybe_reload()
        out = []
        for raw in self._version(project_id, version_id).get("components", []):
            c = _component(raw)
            c.write_key = f"{project_id}|{version_id}|{c.external_id}"
            out.append(c)
        return out

    def source_coordinates(self, project_id: str, version_id: str) -> list[dict[str, Any]]:
        """Registry identity per component, exactly as a vendor BOM reports it: a purl, an origin
        string, and a download URL when the BOM already knows where the source lives."""
        self._maybe_reload()
        out = []
        for raw in self._version(project_id, version_id).get("components", []):
            origins = []
            if raw.get("purl"):
                origins.append({"purl": raw["purl"], "origin": None, "external": None})
            if raw.get("origin"):
                origins.append({"purl": None, "origin": raw["origin"], "external": raw["origin"]})
            out.append({"name": raw.get("name"), "version": raw.get("version"),
                        "origins": origins or [{}], "download_url": raw.get("download_url"),
                        "bom_href": None})
        return out

    def version_label(self, project_id: str, version_id: str) -> dict[str, str]:
        self._maybe_reload()
        for p in self.projects:
            if p["id"] == project_id:
                for v in p.get("versions", []):
                    if v["id"] == version_id:
                        return {"project": p.get("name", p["id"]), "version": v.get("name", v["id"])}
        raise ConnectorError(f"project version {project_id}/{version_id} not found")

    def read_component(self, key: str) -> dict[str, Any]:
        c = self._find(key)
        return {
            "licenses": [l["name"] for l in c.get("licenses", [])],
            "copyrights": [x["text"] for x in c.get("copyrights", [])],
            "license_texts": [{"kind": t.get("kind"), "text": t["text"],
                               "file_name": t.get("file_name"),
                               "content_hash": t.get("content_hash") or sha256(t["text"])}
                              for t in c.get("license_texts", [])],
        }

    def _guard(self, key: str) -> None:
        c = self._find(key)
        if c.get("fail_writes_once") and key not in self._fail_once:
            self._fail_once.add(key)
            raise ConnectorError("simulated PL API failure (500)", status=500)

    def update_licenses(self, key: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        self._guard(key)
        c = self._find(key)
        c["licenses"] = [{"name": e["name"], "spdx": e.get("spdx"), "license_type": e.get("license_type", "declared")}
                         for e in entries]
        return {"status": 200, "body": {"licenses": [e["name"] for e in entries]}}

    def update_copyrights(self, key: str, notices: list[str]) -> dict[str, Any]:
        self._guard(key)
        c = self._find(key)
        c["copyrights"] = [{"text": t, "origin": "synchronized from SL evidence"} for t in notices]
        return {"status": 200, "body": {"copyrights": notices}}

    def update_license_text(self, key: str, text: str, file_name: str | None = None) -> dict[str, Any]:
        self._guard(key)
        c = self._find(key)
        c["license_texts"] = [{"kind": "source_file", "text": text, "file_name": file_name,
                               "provenance": "synchronized from SL evidence",
                               "content_hash": sha256(text), "is_license_file": True}]
        return {"status": 200, "body": {"content_hash": sha256(text)}}


class MockSLConnector:
    tool = "mock-sl"
    capabilities = Capabilities(
        read_components=True, read_copyrights=True, read_license_text=True,
        write_copyrights=False, write_license_names=False, write_license_text=False,
        verify_writes=False, notes="evidence source only",
    )

    def __init__(self, base_url: str = "", config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None):
        cfg = config or {}
        self._path = cfg.get("fixtures", "")
        data = _load(self._path, "sl.json")
        self._mtime = os.path.getmtime(_load_path(self._path, "sl.json"))
        self.scans: list[dict[str, Any]] = copy.deepcopy(data.get("scans", []))
        self.folders: list[dict[str, Any]] = copy.deepcopy(
            data.get("folders", [{"id": 1, "name": "Unpacked", "parent": 0}]))
        self.uploads: dict[int, list[dict[str, Any]]] = {int(f["id"]): [] for f in self.folders}

    def _maybe_reload(self) -> None:
        path = _load_path(self._path, "sl.json")
        mtime = os.path.getmtime(path)
        if mtime != self._mtime:
            self._mtime = mtime
            data = _load(self._path, "sl.json")
            self.scans = copy.deepcopy(data.get("scans", []))
            self.folders = copy.deepcopy(data.get("folders", [{"id": 1, "name": "Unpacked", "parent": 0}]))
            self.uploads = {int(f["id"]): [] for f in self.folders}

    def test_connection(self) -> dict[str, Any]:
        return {"ok": True, "tool": self.tool, "scans": len(self.scans)}

    def list_scans(self) -> list[ScanInfo]:
        self._maybe_reload()
        return [ScanInfo(external_id=s["id"], name=s.get("name", s["id"]), date=s.get("date"),
                         description=s.get("description")) for s in self.scans]

    def fetch_inventory(self, scan_id: str) -> list[ComponentData]:
        self._maybe_reload()
        for s in self.scans:
            if s["id"] == scan_id:
                return [_component(c) for c in s.get("components", [])]
        raise ConnectorError(f"scan {scan_id!r} not found")

    # --- deploy side: a folder of one upload per library, same shape as FOSSology
    def list_folders(self) -> list[dict[str, Any]]:
        self._maybe_reload()
        return copy.deepcopy(self.folders)

    def ensure_folder(self, name: str, parent_id: int) -> dict[str, Any]:
        for folder in self.folders:
            if folder["name"] == name and folder.get("parent") == parent_id:
                return {"id": folder["id"], "name": name, "created": False}
        fid = max([f["id"] for f in self.folders], default=0) + 1
        self.folders.append({"id": fid, "name": name, "parent": parent_id})
        self.uploads.setdefault(fid, [])
        return {"id": fid, "name": name, "created": True}

    def list_uploads(self, folder_id: int | str) -> list[dict[str, Any]]:
        self._maybe_reload()
        try:
            fid = int(folder_id)
        except (TypeError, ValueError):
            # ValueError, not ConnectorError: the API maps it to a 400 for a bad id.
            raise ValueError(f"folder id must be a number, got {folder_id!r}") from None
        return [{"id": u["id"], "uploadname": u["name"]} for u in self.uploads.get(fid, [])]

    def upload_url(self, folder_id: int | str, url: str, name: str, main_license: str | None = None,
                   agents: list[str] | None = None) -> dict[str, Any]:
        fid = int(folder_id)
        bucket = self.uploads.setdefault(fid, [])
        uid = max([u["id"] for u in bucket], default=0) + 1
        bucket.append({"id": uid, "name": name, "url": url, "agents": list(agents or []),
                       "main_license": main_license})
        return {"status": 201, "id": uid, "message": "Upload is created"}
