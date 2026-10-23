"""Black Duck (Hub REST API) project-level connector.

Reads BOM components, license declarations, KB license text and origin
copyrights. Writes are limited to what the documented API demonstrates:
license declarations via a PUT on the BOM component resource. Copyright and
component license text writes are NOT exposed by the API and are reported as
unsupported instead of being faked.
"""
from __future__ import annotations

from typing import Any

from ..domain import Capabilities, ComponentData, CopyrightEntry, LicenseEntry, LicenseTextCandidate, ProjectInfo, norm_name
from .base import ConnectorError, credential
from .http import HttpError, UnsupportedOperation, get_json, get_text, join, paginated, quote, request


def _link(obj: dict[str, Any], rel: str) -> str | None:
    meta = obj.get("_meta") or {}
    for link in meta.get("links") or []:
        if link.get("rel") == rel and link.get("href"):
            return link["href"]
    val = obj.get(rel)
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("href") or (val.get("_meta") or {}).get("href")
    return None


class BlackDuckConnector:
    tool = "blackduck"
    capabilities = Capabilities(
        read_components=True,
        read_copyrights=True,
        read_license_text=True,
        write_copyrights=False,
        write_license_names=True,  # best effort, verified after write
        write_license_text=False,
        verify_writes=True,
        notes=(
            "Copyright statements are read-only through this API; PUT {origin}/copyrights-refresh only "
            "re-requests origin data. Component license text is the KB canonical text (read-only). "
            "License declarations can be written by PUT on the BOM component resource; the vendor "
            "example demonstrates this for SUB_PROJECT components, so writes are best-effort and verified."
        ),
    )

    def __init__(self, base_url: str = "", config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None):
        cfg = config or {}
        env = auth_env or {}
        self.base_url = (base_url or cfg.get("base_url") or "").rstrip("/")
        self.token = credential(cfg, env, "token")
        self.timeout = float(cfg.get("timeout", 30))
        self.retries = int(cfg.get("retries", 2))
        self._license_cache: dict[str, dict[str, Any]] = {}
        self._text_cache: dict[str, str] = {}

    # --- transport helpers
    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise ConnectorError("Black Duck token missing (set the environment variable named in auth_env.token)")
        return {"Authorization": f"Bearer {self.token}"}

    def _get(self, url: str) -> Any:
        try:
            return get_json(url, headers=self._headers(), timeout=self.timeout, retries=self.retries)
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc

    def _get_text(self, url: str) -> str:
        try:
            return get_text(url, headers=self._headers(), timeout=self.timeout, retries=self.retries)
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc

    def _paginate(self, url: str, limit: int = 100) -> list[dict[str, Any]]:
        def page(n: int) -> tuple[list[dict[str, Any]], int | None]:
            sep = "&" if "?" in url else "?"
            data = self._get(f"{url}{sep}limit={limit}&offset={n * limit}")
            if isinstance(data, list):
                return data, None
            return list(data.get("items", [])), data.get("totalCount")

        return paginated(page, limit)

    # --- contract
    def test_connection(self) -> dict[str, Any]:
        data = self._get(join(self.base_url, "/api/projects?limit=1&offset=0"))
        return {"ok": True, "tool": self.tool, "total_projects": data.get("totalCount", 0)}

    def list_projects(self) -> list[ProjectInfo]:
        items = self._paginate(join(self.base_url, "/api/projects"))
        out: list[ProjectInfo] = []
        for p in items:
            href = (p.get("_meta") or {}).get("href") or ""
            out.append(ProjectInfo(external_id=href.rsplit("/", 1)[-1], name=p.get("name", ""), href=href))
        return out

    def list_versions(self, project_id: str) -> list[str]:
        items = self._paginate(join(self.base_url, f"/api/projects/{quote(project_id)}/versions"))
        return [v.get("versionName", "") for v in items]

    def fetch_inventory(self, project_id: str, version_id: str) -> list[ComponentData]:
        url = join(self.base_url, f"/api/projects/{quote(project_id)}/versions/{quote(version_id)}/components")
        raw_items = self._paginate(url)
        out: list[ComponentData] = []
        seen: dict[str, int] = {}
        for raw in raw_items:
            name = raw.get("componentName", "unknown")
            version = raw.get("componentVersionName")
            key = f"{name}|{version or ''}"
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key = f"{key}#{((raw.get('_meta') or {}).get('href') or '').rsplit('/', 1)[-1]}"
            licenses, texts = self._licenses(raw.get("licenses", []))
            out.append(
                ComponentData(
                    external_id=key,
                    name=name,
                    version=version,
                    purl=None,
                    metadata={"bom_href": (raw.get("_meta") or {}).get("href"),
                              "origin_name": raw.get("componentVersionOriginName"),
                              "component_type": raw.get("componentType"),
                              "external_ids": raw.get("inputExternalIds") or []},
                    copyrights=self._copyrights(raw),
                    licenses=licenses,
                    license_texts=texts,
                    write_key=(raw.get("_meta") or {}).get("href"),
                    raw=raw,
                )
            )
        return out

    def read_component(self, key: str) -> dict[str, Any]:
        raw = self._get(key)
        names: list[str] = []
        for entry in raw.get("licenses", []):
            names.extend(self._license_names(entry))
        return {"licenses": names, "license_texts": []}

    def update_licenses(self, key: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        raw = self._get(key)
        resolved = []
        for e in entries:
            href = self._resolve_license(e["name"])
            resolved.append({"licenseDisplay": e["name"], "license": href})
        body = dict(raw)
        body["licenses"] = (
            [{"licenseType": "CONJUNCTIVE", "licenses": resolved}] if len(resolved) > 1 else resolved
        )
        try:
            status, parsed = request(
                "PUT", key, headers=self._headers(), body=body,
                timeout=self.timeout, retries=self.retries,
            )
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc
        if status >= 400:
            raise ConnectorError(f"PUT {key} failed with {status}", status=status)
        return {"status": status, "body": parsed}

    def update_copyrights(self, key: str, notices: list[str]) -> dict[str, Any]:
        raise UnsupportedOperation(
            "Black Duck API has no documented endpoint to set component copyright statements "
            "(component-origin-copyrights is read-only; copyrights-refresh only re-requests origin data)"
        )

    def update_license_text(self, key: str, text: str, file_name: str | None = None) -> dict[str, Any]:
        raise UnsupportedOperation(
            "Black Duck API exposes only the canonical KB license text (license/text link), read-only; "
            "it has no documented endpoint to store component-specific license text"
        )

    # --- license helpers
    def _license_detail(self, href: str) -> dict[str, Any]:
        if href not in self._license_cache:
            self._license_cache[href] = self._get(href)
        return self._license_cache[href]

    def _license_names(self, entry: dict[str, Any]) -> list[str]:
        if entry.get("license"):
            detail = self._license_detail(entry["license"])
            return [detail.get("name") or entry.get("licenseDisplay") or "unknown"]
        names: list[str] = []
        for sub in entry.get("licenses", []) or []:
            names.extend(self._license_names(sub))
        return names

    def _licenses(self, entries: list[dict[str, Any]], depth: int = 0) -> tuple[list[LicenseEntry], list[LicenseTextCandidate]]:
        out: list[LicenseEntry] = []
        texts: list[LicenseTextCandidate] = []
        for entry in entries or []:
            if depth > 4:
                break
            if entry.get("licenses") and isinstance(entry.get("licenses"), list):
                ltype = "alternative" if str(entry.get("licenseType", "")).upper() == "DISJUNCTIVE" else "conjunctive"
                sub_lic, sub_txt = self._licenses(entry["licenses"], depth + 1)
                for lic in sub_lic:
                    lic.license_type = ltype
                out.extend(sub_lic)
                texts.extend(sub_txt)
                continue
            if not entry.get("license"):
                continue
            detail = self._license_detail(entry["license"])
            name = detail.get("name") or entry.get("licenseDisplay") or "unknown"
            out.append(LicenseEntry(name=name, spdx=detail.get("spdxId"), source="blackduck",
                                    license_type=entry.get("licenseType", "declared").lower()))
            text_url = _link(detail, "text")
            if text_url:
                if text_url not in self._text_cache:
                    try:
                        self._text_cache[text_url] = self._get_text(text_url)
                    except ConnectorError:
                        self._text_cache[text_url] = ""
                body = self._text_cache[text_url]
                if body:
                    texts.append(
                        LicenseTextCandidate(
                            kind="generic_metadata",
                            text=body,
                            provenance="blackduck license metadata (KB canonical text)",
                            file_name=None,
                            file_path=None,
                        )
                    )
        return out, texts

    def _resolve_license(self, name: str) -> str:
        data = self._get(join(self.base_url, f"/api/licenses?limit=100&offset=0&q=name:{quote(name)}"))
        items = data.get("items", []) if isinstance(data, dict) else data
        for item in items:
            if norm_name(item.get("name", "")) == norm_name(name):
                href = _link(item, "self") or (item.get("_meta") or {}).get("href")
                if href:
                    return href
        raise ConnectorError(f"license {name!r} not found in the Black Duck knowledge base")

    # --- copyright helpers
    def _copyrights(self, raw: dict[str, Any]) -> list[CopyrightEntry]:
        out: list[CopyrightEntry] = []
        seen: set[str] = set()
        for origin in raw.get("origins", []) or []:
            if isinstance(origin, str):
                try:
                    origin = self._get(origin)
                except ConnectorError:
                    continue
            if not isinstance(origin, dict):
                continue
            href = _link(origin, "component-origin-copyrights") or ((origin.get("_meta") or {}).get("href") or "")
            if not href:
                continue
            if "component-origin-copyrights" not in href:
                href = href.rstrip("/") + "/component-origin-copyrights"
            try:
                items = self._paginate(href)
            except ConnectorError:
                continue
            origin_name = origin.get("originName", "origin")
            for item in items:
                text = item.get("updatedCopyright") or item.get("kbCopyright") or ""
                if not text or text in seen:
                    continue
                seen.add(text)
                out.append(
                    CopyrightEntry(
                        text=text,
                        origin=origin_name,
                        source_path=item.get("filePath"),
                        active=bool(item.get("active", True)),
                    )
                )
        return out
