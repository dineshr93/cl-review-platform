"""ScanCode Toolkit source-level connector (JSON output file).

ScanCode's `--json-pp` output carries file paths, license detections and
copyrights. It does not carry file contents by default, so source-file license
text is only available when the file record includes `text`/`file_content`;
otherwise the component is flagged for review (never reconstructed).
"""
from __future__ import annotations

import json
import os
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
from .http import UnsupportedOperation


class ScanCodeConnector:
    tool = "scancode"
    capabilities = Capabilities(
        read_components=True,
        read_copyrights=True,
        read_license_text=True,
        write_copyrights=False,
        write_license_names=False,
        write_license_text=False,
        verify_writes=False,
        notes="reads a ScanCode JSON file; no license text unless the file record carries content",
    )

    def __init__(self, base_url: str = "", config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None):
        cfg = config or {}
        env = auth_env or {}
        self.path = credential(cfg, env, "json_path") or cfg.get("json_path")
        if not self.path:
            raise ConnectorError("ScanCode connector requires config.json_path (or its env variable)")
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            if not os.path.exists(self.path):
                raise ConnectorError(f"ScanCode JSON not found: {self.path}")
            with open(self.path, encoding="utf-8") as fh:
                self._data = json.load(fh)
        return self._data

    def test_connection(self) -> dict[str, Any]:
        data = self._load()
        return {"ok": True, "tool": self.tool, "files": len(data.get("files", []))}

    def list_scans(self) -> list[ScanInfo]:
        data = self._load()
        return [ScanInfo(external_id=os.path.basename(self.path), name=os.path.basename(self.path),
                         description=f"ScanCode JSON: {len(data.get('files', []))} files")]

    def fetch_inventory(self, scan_id: str) -> list[ComponentData]:
        data = self._load()
        packages = data.get("packages") or []
        if packages:
            return [self._package(p) for p in packages]
        return self._grouped_files(data.get("files") or [])

    def read_component(self, key: str) -> dict[str, Any]:
        raise UnsupportedOperation("ScanCode is an evidence source; the platform never reads PL values from it")

    def update_licenses(self, key: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        raise UnsupportedOperation("ScanCode is an evidence source; synchronization flows SL -> PL only")

    def update_copyrights(self, key: str, notices: list[str]) -> dict[str, Any]:
        raise UnsupportedOperation("ScanCode is an evidence source; synchronization flows SL -> PL only")

    def update_license_text(self, key: str, text: str, file_name: str | None = None) -> dict[str, Any]:
        raise UnsupportedOperation("ScanCode is an evidence source; synchronization flows SL -> PL only")

    def _package(self, pkg: dict[str, Any]) -> ComponentData:
        name = pkg.get("package_name") or pkg.get("name") or "unknown"
        version = pkg.get("package_version") or pkg.get("version")
        licenses = []
        expr = pkg.get("license_expression")
        if expr:
            for part in str(expr).replace(" AND ", " ").replace(" OR ", " ").split():
                licenses.append(LicenseEntry(name=part.strip("()"), spdx=part.strip("()"), source="scancode"))
        return ComponentData(external_id=f"{name}|{version or ''}", name=name, version=version,
                             purl=pkg.get("purl"), metadata={"declared_license": expr},
                             licenses=licenses, raw=pkg)

    def _grouped_files(self, files: list[dict[str, Any]]) -> list[ComponentData]:
        tops = {f.get("path", "").split("/", 1)[0] for f in files if "/" in f.get("path", "")}
        single = len(tops) == 1
        single_key = next(iter(tops)) if tops else "root"
        groups: dict[str, dict[str, Any]] = {}
        for f in files:
            path = f.get("path") or ""
            key = single_key if single else (path.split("/", 1)[0] if "/" in path else "root")
            g = groups.setdefault(key, {"paths": [], "licenses": {}, "copyrights": {}, "texts": []})
            g["paths"].append(path)
            for det in f.get("license_detections") or []:
                expr = det.get("license_expression") or det.get("license") or ""
                for part in str(expr).replace(" AND ", " ").replace(" OR ", " ").split():
                    g["licenses"].setdefault(path, []).append(part.strip("()"))
            for c in f.get("copyrights") or []:
                if c.get("value"):
                    g["copyrights"].setdefault(path, []).append(c["value"])
            body = f.get("text") or f.get("file_content")
            if body and is_license_file(path):
                g["texts"].append((path, str(body)))
        out: list[ComponentData] = []
        for key, g in groups.items():
            name, version = split_component_dir(key)
            licenses: dict[str, LicenseEntry] = {}
            for path, names in g["licenses"].items():
                for n in names:
                    lic = licenses.setdefault(n, LicenseEntry(name=n, source="scancode"))
                    lic.evidence_paths.append(path)
            copyrights = [
                CopyrightEntry(text=t, origin="scancode copyright detection", source_path=p)
                for p, texts in g["copyrights"].items() for t in texts
            ]
            texts = [
                LicenseTextCandidate(kind="source_file", text=body, file_name=path.rsplit("/", 1)[-1],
                                     file_path=path, content_hash=sha256(body), is_license_file=True,
                                     provenance="scancode file record content")
                for path, body in g["texts"]
            ]
            out.append(ComponentData(external_id=key, name=name, version=version,
                                     metadata={"source_files": g["paths"]},
                                     copyrights=copyrights, licenses=list(licenses.values()),
                                     license_texts=texts, source_files=g["paths"], raw={"group": key}))
        return out
