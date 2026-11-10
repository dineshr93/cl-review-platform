"""Tiny YAML-subset loader for config.yaml profiles.

The project stays stdlib-only: this parser accepts mappings, nested mappings,
and lists, which is the shape the sample config uses. It refuses a malformed
value instead of silently treating it as text.
"""
from __future__ import annotations

import os
import re
from typing import Any


def _strip_comment(line: str) -> str:
    quote = False
    out = []
    for ch in line:
        if ch in ("'", '"'):
            quote = not quote
        if ch == "#" and not quote:
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_key(line: str) -> tuple[str, str]:
    quote = False
    for i, ch in enumerate(line):
        if ch in ("'", '"'):
            quote = not quote
        elif ch == ":" and not quote:
            key = line[:i].strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
                key = key[1:-1]
            return key, line[i + 1:].strip()
    key = line.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        key = key[1:-1]
    return key, ""


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if value == "{}":
        return {}
    if value == "[]":
        return []
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def load_config(path: str) -> dict[str, Any]:
    """Parse the config.yaml subset. A missing file is a clear caller error."""
    if not os.path.exists(path):
        raise ValueError(f"config file not found: {path}")
    tokens: list[tuple[int, str]] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = _strip_comment(raw)
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            tokens.append((indent, line.strip()))

    root: dict[str, Any] = {}
    stack: list[tuple[Any, str | None, int]] = [(root, None, -1)]

    for indent, content in tokens:
        while indent <= stack[-1][2]:
            stack.pop()
        parent = stack[-1][0]

        if content.startswith("- "):
            item_text = content[2:].strip()
            if isinstance(parent, dict):
                container, key, _ = stack.pop()
                grand = stack[-1][0]
                grand[key] = []
                parent = grand[key]
            if ":" in item_text:
                item: dict[str, Any] = {}
                k, v = _split_key(item_text)
                item[k] = _scalar(v)
                parent.append(item)
                stack.append((item, None, indent))
            else:
                parent.append(_scalar(item_text))
            continue

        key, rest = _split_key(content)
        if not key:
            raise ValueError(f"unparseable config line: {content!r}")
        if rest:
            parent[key] = _scalar(rest)
        else:
            parent[key] = {}
            stack.append((parent[key], key, indent))

    return root


def config_profiles(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("profiles", config)


def profile_connection(profile: dict[str, Any], role: str) -> dict[str, Any]:
    """Return the connection profile for role, with secrets named, never carried."""
    block = profile.get(role) or {}
    auth_env = block.get("auth_env") or {}
    config = dict(block.get("config") or {})
    return {
        "role": role,
        "tool": block.get("tool", "blackduck" if role == "pl" else "fossology"),
        "name": block.get("name", f"{role} profile"),
        "base_url": block.get("url", ""),
        "config": config,
        "auth_env": auth_env,
    }
