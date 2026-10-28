"""Black Duck (Hub REST API) project-level connector.

Reads BOM components, license declarations, KB license text and origin
copyrights. Writes are limited to what the Black Duck REST reference
(`apis/blackduck_Blackduck-REST-API-v2026.4.2.md`) demonstrates:

* license declarations — PUT on the BOM component resource
* copyright statements — POST on the component-version-origin copyright collection
* component license text — PUT on the BOM component license ``/text`` resource

Anything the reference does not document is reported unsupported, never faked.
"""
from __future__ import annotations

import json
from typing import Any

from ..domain import (Capabilities, ComponentData, CopyrightEntry, LicenseEntry, LicenseTextCandidate,
                      ProjectInfo, norm_name, sha256)
from .base import ConnectorError, credential
from .http import (HttpError, get_json, get_text, join, paginated, put_json, quote,
                   request)

# Media types named by the REST reference for the resources this connector writes.
BOM_MEDIA = "application/vnd.blackducksoftware.bill-of-materials-7+json"
COPYRIGHT_MEDIA = "application/vnd.blackducksoftware.copyright-4+json"


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
        write_copyrights=True,
        write_license_names=True,  # best effort, verified after write
        write_license_text=True,
        verify_writes=True,
        notes=(
            "Copyrights are stored per component-version origin: new statements are POSTed to the "
            "origin copyright collection (source CUSTOM); existing ones are never overwritten. "
            "Component license text is written on the BOM component license /text resource, which is "
            "the component-specific override of the KB canonical text (restore is a separate Black Duck "
            "endpoint this connector does not call). License declarations are written by PUT on the BOM "
            "component resource; the vendor example demonstrates this for SUB_PROJECT components, so "
            "writes are best-effort and verified."
        ),
    )

    def __init__(self, base_url: str = "", config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None):
        cfg = config or {}
        env = auth_env or {}
        self.base_url = (base_url or cfg.get("base_url") or "").rstrip("/")
        self.token = credential(cfg, env, "token")
        self.timeout = float(cfg.get("timeout", 30))
        self.retries = int(cfg.get("retries", 2))
        self.config = dict(cfg)
        self._license_cache: dict[str, dict[str, Any]] = {}
        self._text_cache: dict[str, str] = {}

    # --- transport helpers
    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        if not self.token:
            raise ConnectorError("Black Duck token missing (set the environment variable named in auth_env.token)")
        return {"Authorization": f"Bearer {self.token}", "Accept": accept}

    def _abs(self, url: str) -> str:
        """Black Duck representations mix absolute hrefs and root-relative links."""
        return url if url.startswith("http") else join(self.base_url, url)

    def _send(self, method: str, url: str, body: Any, media: str) -> dict[str, Any]:
        try:
            status, _, data = request(
                method, url, headers={**self._headers(media), "Content-Type": media}, body=body,
                timeout=self.timeout, retries=self.retries,
            )
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc
        if status >= 400:
            raise ConnectorError(f"{method} {url} failed with {status}", status=status)
        text = data.decode("utf-8", "replace") if data else ""
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = text
        return {"status": status, "body": parsed}

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

    def source_coordinates(self, project_id: str, version_id: str) -> list[dict[str, Any]]:
        """Registry coordinates of every BOM component — the input of a source resolver.

        Only identity is read here (no license detail calls), so this stays cheap on a large BOM.
        """
        url = join(self.base_url, f"/api/projects/{quote(project_id)}/versions/{quote(version_id)}/components")
        out: list[dict[str, Any]] = []
        for raw in self._paginate(url):
            origins: list[dict[str, Any]] = []
            for origin in raw.get("origins", []) or []:
                if isinstance(origin, str):
                    origins.append({"origin": origin})
                    continue
                external = ":".join(x for x in (origin.get("externalNamespace"), origin.get("externalId")) if x)
                origins.append({"purl": origin.get("packageUrl"), "origin": origin.get("origin"),
                                "external": external or None})
            if not origins:
                origins.append({})
            out.append({"name": raw.get("componentName", ""),
                        "version": raw.get("componentVersionName"),
                        "origins": origins,
                        "bom_href": (raw.get("_meta") or {}).get("href")})
        return out

    def version_label(self, project_id: str, version_id: str) -> dict[str, str]:
        """Display names for a project version, used for folder naming instead of opaque ids."""
        project = self._get(join(self.base_url, f"/api/projects/{quote(project_id)}"))
        version = self._get(join(self.base_url, f"/api/projects/{quote(project_id)}/versions/{quote(version_id)}"))
        return {"project": project.get("name", project_id), "version": version.get("versionName", version_id)}

    def read_component(self, key: str) -> dict[str, Any]:
        raw = self._get(key)
        names: list[str] = []
        for entry in raw.get("licenses", []):
            names.extend(self._license_names(entry))
        texts: list[dict[str, Any]] = []
        for lic in self._bom_licenses(raw):
            try:
                body = self._get_text(self._abs(lic["href"]) + "/text")
            except ConnectorError:
                continue
            if body:
                texts.append({"kind": "bom_component_license", "text": body,
                              "file_name": lic["name"] or None, "content_hash": sha256(body)})
        return {"licenses": names, "copyrights": [c.text for c in self._copyrights(raw)],
                "license_texts": texts}

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
            status, parsed = put_json(key, body, headers=self._headers(),
                                      timeout=self.timeout, retries=self.retries)
        except HttpError as exc:
            raise ConnectorError(str(exc), status=exc.status) from exc
        if status >= 400:
            raise ConnectorError(f"PUT {key} failed with {status}", status=status)
        return {"status": status, "body": parsed}

    def update_copyrights(self, key: str, notices: list[str]) -> dict[str, Any]:
        """Create the copyright statements Black Duck does not already store.

        Copyrights live on a component-version origin, so the statements are POSTed to the first
        origin's copyright collection. Existing statements are left untouched: Black Duck has no
        documented delete for a copyright, so a removed review value is never silently dropped.
        """
        raw = self._get(key)
        collections = self._copyright_collections(raw)
        if not collections:
            raise ConnectorError("BOM component has no origins; Black Duck stores copyrights per origin")
        url = collections[0]["url"]
        existing = {c.text for c in self._copyrights(raw)}
        created: list[str] = []
        last: dict[str, Any] = {"status": 200, "body": {"numberOfUpdates": 0}}
        for notice in notices:
            if not notice or notice in existing:
                continue
            last = self._send("POST", url, {"copyright": notice}, COPYRIGHT_MEDIA)
            created.append(notice)
        return {"status": last["status"], "body": {"created": created, "copyright_collection": url}}

    def update_license_text(self, key: str, text: str, file_name: str | None = None) -> dict[str, Any]:
        """Store the component-specific license text on the BOM component license resource."""
        raw = self._get(key)
        licenses = self._bom_licenses(raw)
        if not licenses:
            raise ConnectorError("BOM component declares no license; there is no license text resource to update")
        target = licenses[0] if len(licenses) == 1 else self._pick_license(licenses, file_name)
        return self._send("PUT", self._abs(target["href"]) + "/text", {"text": text}, BOM_MEDIA)

    @staticmethod
    def _pick_license(licenses: list[dict[str, Any]], file_name: str | None) -> dict[str, Any]:
        needle = norm_name(file_name or "")
        matches = [l for l in licenses if norm_name(l["name"]) and norm_name(l["name"]) in needle]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(l["name"] or "?" for l in licenses)
        raise ConnectorError(
            f"BOM component declares {len(licenses)} licenses ({names}); "
            f"cannot decide which license text {file_name or 'the reviewed file'} belongs to"
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

    def _bom_licenses(self, raw: dict[str, Any]) -> list[dict[str, str]]:
        """BOM component license resources (`.../components/{c}/licenses/{id}`), flattened."""
        out: list[dict[str, str]] = []

        def walk(entries: list[dict[str, Any]]) -> None:
            for entry in entries or []:
                if entry.get("licenses"):
                    walk(entry["licenses"])
                if entry.get("license"):
                    out.append({"name": entry.get("licenseDisplay") or "", "href": entry["license"]})

        walk(raw.get("licenses", []))
        return out

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
    def _copyright_collections(self, raw: dict[str, Any]) -> list[dict[str, str]]:
        """Resolve each BOM origin to its copyright collection resource.

        The collection answers GET, POST and PUT (create / update / bulk update), which is what
        `update_copyrights` needs; the origin resource itself only offers the read link.
        """
        out: list[dict[str, str]] = []
        for origin in raw.get("origins", []) or []:
            url = origin if isinstance(origin, str) else (origin.get("origin") or origin.get("href"))
            if not url:
                continue
            name = (origin.get("name") if isinstance(origin, dict) else None) or "origin"
            if isinstance(origin, str):
                try:
                    detail = self._get(self._abs(url))
                except ConnectorError:
                    continue
                name = detail.get("originName") or detail.get("externalId") or name
                url = _link(detail, "component-origin-copyrights") or url
            href = self._abs(url)
            if "copyrights" not in href:
                href = href.rstrip("/") + "/copyrights"
            out.append({"origin": name, "url": href})
        return out

    def _copyrights(self, raw: dict[str, Any]) -> list[CopyrightEntry]:
        out: list[CopyrightEntry] = []
        seen: set[str] = set()
        for entry in self._copyright_collections(raw):
            href, origin_name = entry["url"], entry["origin"]
            try:
                items = self._paginate(href)
            except ConnectorError:
                continue
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
