"""FOSSID project-level adapter placeholder.

Registered so the role is visible in the tool catalog, but every operation
fails with an explicit limitation. No FOSSID API capability is invented; the
core review workflow does not change when a real adapter replaces this file.
"""
from __future__ import annotations

from typing import Any

from ..domain import Capabilities, ComponentData, ProjectInfo
from .base import ConnectorError, NoWriteEndpoints


class FossidConnector(NoWriteEndpoints):
    tool = "fossid"
    evidence_role = "FOSSID adapter not implemented"
    capabilities = Capabilities(
        read_components=False,
        read_copyrights=False,
        read_license_text=False,
        write_copyrights=False,
        write_license_names=False,
        write_license_text=False,
        verify_writes=False,
        notes="adapter not implemented: FOSSID API surface not verified against a live instance",
    )

    def __init__(self, base_url: str = "", config: dict[str, Any] | None = None, auth_env: dict[str, str] | None = None):
        self.base_url = base_url
        self.config = config or {}

    def _not_implemented(self, op: str) -> ConnectorError:
        return ConnectorError(f"FOSSID adapter not implemented for {op} (no verified API mapping)", unsupported=True)

    def test_connection(self) -> dict[str, Any]:
        raise self._not_implemented("test_connection")

    def list_projects(self) -> list[ProjectInfo]:
        raise self._not_implemented("list_projects")

    def list_versions(self, project_id: str) -> list[str]:
        raise self._not_implemented("list_versions")

    def fetch_inventory(self, project_id: str, version_id: str) -> list[ComponentData]:
        raise self._not_implemented("fetch_inventory")

    def read_component(self, key: str) -> dict[str, Any]:
        # Not "this tool has no write API" but "this adapter is a placeholder": every FOSSID
        # operation, read included, is unimplemented, so the shared refusal does not apply.
        raise self._not_implemented("read_component")
