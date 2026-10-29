"""Connector contracts and the tool/role registry.

Roles are configuration, not vendor identity: any registered tool can be used
as a project-level (PL) or source-level (SL) connector if it declares that role.
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

from ..domain import Capabilities, ComponentData, ProjectInfo, ScanInfo
from .http import UnsupportedOperation


class ConnectorError(Exception):
    def __init__(self, message: str, *, status: int | None = None, unsupported: bool = False):
        self.status = status
        self.unsupported = unsupported
        super().__init__(message)


@runtime_checkable
class ProjectLevelConnector(Protocol):
    tool: str
    capabilities: Capabilities

    def test_connection(self) -> dict[str, Any]: ...
    def list_projects(self) -> list[ProjectInfo]: ...
    def list_versions(self, project_id: str) -> list[str]: ...
    def fetch_inventory(self, project_id: str, version_id: str) -> list[ComponentData]: ...
    def read_component(self, key: str) -> dict[str, Any]: ...
    def update_licenses(self, key: str, entries: list[dict[str, Any]]) -> dict[str, Any]: ...
    def update_copyrights(self, key: str, notices: list[str]) -> dict[str, Any]: ...
    def update_license_text(self, key: str, text: str, file_name: str | None = None) -> dict[str, Any]: ...


@runtime_checkable
class SourceLevelConnector(Protocol):
    tool: str
    capabilities: Capabilities

    def test_connection(self) -> dict[str, Any]: ...
    def list_scans(self) -> list[ScanInfo]: ...
    def fetch_inventory(self, scan_id: str) -> list[ComponentData]: ...


def credential(config: dict[str, Any], auth_env: dict[str, str], key: str) -> str | None:
    """Read a credential from the environment named in the connection config.

    The ``config`` fallback serves connectors constructed directly (tests, scripts);
    a persisted connection never holds a secret - ``service.add_connection`` rejects a
    credential-bearing config key, so credentials reach a connector only via ``auth_env``.
    """
    env_name = auth_env.get(key)
    if env_name:
        return os.environ.get(env_name)
    value = config.get(key)
    return str(value) if value else None


class NoWriteEndpoints:
    """One refusal for a tool that documents no write API.

    A connector sets a single phrase describing what it is and inherits the four refusals.
    The sentences are a user-facing contract - they reach the console as the body of a 409 -
    so they are written once here and asserted verbatim in ``tests/test_connectors.py``.
    """

    evidence_role: str = "this tool"

    def read_component(self, key: str) -> dict[str, Any]:
        raise UnsupportedOperation(f"{self.evidence_role}; the platform never reads PL values from it")

    def update_licenses(self, key: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        raise UnsupportedOperation(f"{self.evidence_role}; synchronization flows SL -> PL only")

    def update_copyrights(self, key: str, notices: list[str]) -> dict[str, Any]:
        raise UnsupportedOperation(f"{self.evidence_role}; synchronization flows SL -> PL only")

    def update_license_text(self, key: str, text: str, file_name: str | None = None) -> dict[str, Any]:
        raise UnsupportedOperation(f"{self.evidence_role}; synchronization flows SL -> PL only")


# --- registry ---------------------------------------------------------------
# A new tool is added here plus one module; the review workflow stays unchanged.


def _factory(cls_path: str) -> Any:
    module, name = cls_path.rsplit(".", 1)

    def build(base_url: str, config: dict[str, Any], auth_env: dict[str, str]) -> Any:
        mod = __import__(module, fromlist=[name])
        return getattr(mod, name)(base_url=base_url or "", config=config or {}, auth_env=auth_env or {})

    return build


REGISTRY: dict[str, dict[str, Any]] = {
    "blackduck": {
        "roles": ("pl",),
        "deploy": True,
        "factory": _factory("clreview.connectors.blackduck.BlackDuckConnector"),
        "description": "Black Duck Hub REST API (project-level components, origin copyrights, KB license text)",
    },
    "fossid": {
        "roles": ("pl",),
        "factory": _factory("clreview.connectors.fossid.FossidConnector"),
        "description": "FOSSID adapter placeholder: role registered, API integration not implemented",
    },
    "fossology": {
        "roles": ("sl",),
        "deploy": True,
        "factory": _factory("clreview.connectors.fossology.FossologyConnector"),
        "description": "FOSSology REST API (upload license/copyright findings, source-file text)",
    },
    "scancode": {
        "roles": ("sl",),
        "factory": _factory("clreview.connectors.scancode.ScanCodeConnector"),
        "description": "ScanCode Toolkit JSON output (components, licenses, copyrights)",
    },
    "mock-pl": {
        "roles": ("pl",),
        "deploy": True,
        "factory": _factory("clreview.connectors.mock.MockPLConnector"),
        "description": "Demo/mock project-level tool; supports all writes for workflow tests",
    },
    "mock-sl": {
        "roles": ("sl",),
        "deploy": True,
        "factory": _factory("clreview.connectors.mock.MockSLConnector"),
        "description": "Demo/mock source-level tool",
    },
}


def create_connector(tool: str, role: str, base_url: str, config: dict[str, Any] | None = None,
                     auth_env: dict[str, str] | None = None) -> Any:
    spec = REGISTRY.get(tool)
    if not spec:
        raise ConnectorError(f"unknown tool {tool!r}; known: {sorted(REGISTRY)}")
    if role not in spec["roles"]:
        raise ConnectorError(f"tool {tool!r} cannot act as {role!r}; supported roles: {spec['roles']}")
    return spec["factory"](base_url, config or {}, auth_env or {})


def tool_catalog() -> list[dict[str, Any]]:
    return [
        {"tool": name, "roles": list(spec["roles"]), "description": spec["description"],
         "deploy": bool(spec.get("deploy"))}
        for name, spec in sorted(REGISTRY.items())
    ]
