"""Folder-name mapping for Black Duck project versions and FOSSology uploads."""
from __future__ import annotations

import re
from typing import Any


def sanitize_folder_name(name: str) -> str:
    cleaned = "".join("_" if c in '\\/:*?"<>|\r\n\t' else c for c in name)
    return "_".join(cleaned.split()).strip("_ .")


def resolve_folder(mapping: dict[str, Any] | None, project: str, version: str | None) -> str:
    """Resolve one folder name from template, per-version override, then regex rules."""
    mapping = mapping or {}
    template = mapping.get("template") or "{bd_project}/{bd_version}"
    key = f"{project}/{version or ''}"
    overrides = mapping.get("overrides") or {}
    raw = overrides.get(key) if key in overrides else template.format(
        bd_project=project, bd_version=version or "", project=project, version=version or "")

    for rule in mapping.get("regex") or []:
        pattern = rule.get("pattern")
        replacement = rule.get("replacement")
        if pattern and replacement is not None:
            raw = re.sub(pattern, replacement, raw)

    if mapping.get("sanitize", True):
        raw = sanitize_folder_name(raw)
    return raw
