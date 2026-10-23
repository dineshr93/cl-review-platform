"""Test helpers: temporary fixture sets, service wiring, a read-only PL stub."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any

from clreview.connectors.base import REGISTRY
from clreview.connectors.http import UnsupportedOperation
from clreview.connectors.mock import MockPLConnector
from clreview.db import DB
from clreview.domain import Capabilities, sha256
from clreview.review import ReviewService
from clreview.service import Service
from clreview.sync import SyncService

GENERIC_ZLIB = "zlib License (canonical SPDX text): generic metadata, not component-specific."
SOURCE_ZLIB = "Copyright (C) 1995-2017 Jean-loup Gailly and Mark Adler\n\nThis software is provided 'as-is'. Permission is granted to use and redistribute it."


def pl_component(**kw: Any) -> dict[str, Any]:
    base = {
        "external_id": kw.get("name", "comp"),
        "name": kw.get("name", "comp"),
        "version": kw.get("version"),
        "purl": kw.get("purl"),
        "metadata": kw.get("metadata", {}),
        "licenses": kw.get("licenses", []),
        "copyrights": kw.get("copyrights", []),
        "license_texts": kw.get("license_texts", []),
        "source_files": kw.get("source_files", []),
    }
    base.update(kw)
    return base


def fixture_set(tmpdir: str, pl_components: list[dict[str, Any]], sl_components: list[dict[str, Any]]) -> dict[str, str]:
    pl = {"projects": [{"id": "proj", "name": "proj", "versions": [{"id": "1.0", "components": pl_components}]}]}
    sl = {"scans": [{"id": "scan", "name": "proj-1.0", "components": sl_components}]}
    pl_path = os.path.join(tmpdir, "pl.json")
    sl_path = os.path.join(tmpdir, "sl.json")
    with open(pl_path, "w", encoding="utf-8") as fh:
        json.dump(pl, fh)
    with open(sl_path, "w", encoding="utf-8") as fh:
        json.dump(sl, fh)
    return {"pl": pl_path, "sl": sl_path}


def make_service(tmpdir: str, pl_components: list[dict[str, Any]], sl_components: list[dict[str, Any]],
                 pl_tool: str = "mock-pl", db_path: str = ":memory:") -> tuple[Service, DB, int]:
    paths = fixture_set(tmpdir, pl_components, sl_components)
    db = DB(db_path)
    svc = Service(db)
    pl = svc.add_connection("pl", pl_tool, "test PL", "", {"fixtures": paths["pl"]}, {})
    sl = svc.add_connection("sl", "mock-sl", "test SL", "", {"fixtures": paths["sl"]}, {})
    svc.test_connection(pl["id"], "test")
    svc.test_connection(sl["id"], "test")
    mapping = svc.create_mapping("test mapping", pl["id"], "proj", "1.0", sl["id"], "scan", "test")
    svc.refresh(mapping["id"], "test")
    return svc, db, mapping["id"]


def rewrite_fixtures(paths: dict[str, str], pl_components: list[dict[str, Any]] | None = None,
                     sl_components: list[dict[str, Any]] | None = None) -> None:
    if pl_components is not None:
        with open(paths["pl"], encoding="utf-8") as fh:
            data = json.load(fh)
        data["projects"][0]["versions"][0]["components"] = pl_components
        with open(paths["pl"], "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    if sl_components is not None:
        with open(paths["sl"], encoding="utf-8") as fh:
            data = json.load(fh)
        data["scans"][0]["components"] = sl_components
        with open(paths["sl"], "w", encoding="utf-8") as fh:
            json.dump(data, fh)


def paths_for(svc: Service, mapping_id: int) -> dict[str, str]:
    m = svc.mapping(mapping_id)
    return {"pl": svc._raw_connection(m["pl_connection_id"])["config"]["fixtures"],
            "sl": svc._raw_connection(m["sl_connection_id"])["config"]["fixtures"]}


class ReadOnlyPL(MockPLConnector):
    """A PL that can read but declares no write capability (like Black Duck copyrights)."""

    capabilities = Capabilities(
        read_components=True, read_copyrights=True, read_license_text=True,
        write_copyrights=False, write_license_names=False, write_license_text=False,
        verify_writes=False, notes="test target: no write endpoints",
    )

    def update_licenses(self, key: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        raise UnsupportedOperation("test target: license writes unsupported")

    def update_copyrights(self, key: str, notices: list[str]) -> dict[str, Any]:
        raise UnsupportedOperation("test target: copyright writes unsupported")

    def update_license_text(self, key: str, text: str, file_name: str | None = None) -> dict[str, Any]:
        raise UnsupportedOperation("test target: license text writes unsupported")


def register_readonly_pl() -> None:
    REGISTRY["readonly-pl"] = {
        "roles": ("pl",),
        "factory": lambda base_url, config, auth_env: ReadOnlyPL(base_url, config, auth_env),
        "description": "test-only read-only project-level connector",
    }


def make_review(svc: Service) -> tuple[ReviewService, SyncService]:
    return ReviewService(svc), SyncService(svc)


def deep_copy(obj: Any) -> Any:
    return copy.deepcopy(obj)


def text_candidate(kind: str, text: str, **kw: Any) -> dict[str, Any]:
    out = {"kind": kind, "text": text, "provenance": kw.get("provenance", "test"), "content_hash": sha256(text)}
    out.update(kw)
    return out


def tmpdir() -> str:
    return tempfile.mkdtemp(prefix="clreview-test-")
