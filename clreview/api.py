"""Thin JSON API + static file server (stdlib only)."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .connectors.base import ConnectorError
from .connectors.http import UnsupportedOperation
from .db import DB
from .review import ReviewService
from .service import Service, now
from .sync import SyncService
from . import __version__

MAX_BODY = 1_000_000
WEB_ROOT = os.path.join(os.path.dirname(__file__), "web")


class Api:
    def __init__(self, db: DB, fixtures_dir: str | None = None):
        self.db = db
        self.service = Service(db, fixtures_dir=fixtures_dir)
        self.review = ReviewService(self.service)
        self.sync = SyncService(self.service)

    # --- dispatch -----------------------------------------------------------
    def health(self) -> dict[str, Any]:
        db_ok = True
        try:
            self.db.query("SELECT 1")
        except Exception:
            db_ok = False
        return {"ok": db_ok, "time": now(), "version": __version__, "db": "ok" if db_ok else "unavailable"}

    def handle(self, method: str, path: str, query: dict[str, list[str]], body: Any, reviewer: str) -> tuple[int, Any]:
        def q(name: str, default: str = "") -> str:
            return (query.get(name) or [default])[0]

        m = re.fullmatch(r"/api/connections/(\d+)/test", path)
        if method == "POST" and m:
            return 200, self.service.test_connection(int(m.group(1)), reviewer)
        m = re.fullmatch(r"/api/connections/(\d+)/(projects|scans|folders)", path)
        if method == "GET" and m:
            cid, kind = int(m.group(1)), m.group(2)
            if kind == "projects":
                return 200, {"items": self.service.list_projects(cid)}
            if kind == "scans":
                return 200, {"items": self.service.list_scans(cid)}
            return 200, {"items": self.service.list_folders(cid)}
        m = re.fullmatch(r"/api/mappings/(\d+)/(refresh|overview)", path)
        if method == "POST" and m and m.group(2) == "refresh":
            return 200, self.service.refresh(int(m.group(1)), reviewer)
        if method == "GET" and m and m.group(2) == "overview":
            return 200, self.service.overview(int(m.group(1)))
        m = re.fullmatch(r"/api/mappings/(\d+)/components", path)
        if method == "GET" and m:
            return 200, self.review.list_components(
                int(m.group(1)), q=q("q"), review_status=q("review_status"), match_status=q("status"),
                confidence=q("confidence"), sort=q("sort", "name"),
                offset=int(q("offset", "0") or 0), limit=min(200, int(q("limit", "50") or 50)),
            )
        m = re.fullmatch(r"/api/suggestions/(\d+)/decision", path)
        if method == "POST" and m:
            return 200, self.review.decide_suggestion(int(m.group(1)), body.get("action", ""), reviewer,
                                                     note=body.get("note", ""))
        m = re.fullmatch(r"/api/components/(\d+)(/(decisions|notes|review|confirm|reassign|reject|suggestions))?", path)
        if m:
            mid, action = int(m.group(1)), m.group(3)
            if method == "GET" and not action:
                return 200, self.review.detail(mid)
            if method == "POST" and action == "suggestions":
                return 200, self.review.add_suggestion(
                    mid, body.get("field", ""), body.get("entry_key", ""), body.get("value", ""),
                    confidence=body.get("confidence", 0.0), model=body.get("model", ""),
                    evidence=body.get("evidence") or {},
                )
            if method == "POST" and action == "decisions":
                return 200, self.review.decide(
                    mid, body.get("field", ""), body.get("entry_key", ""), body.get("action", ""),
                    approved_value=body.get("approved_value"), note=body.get("note", ""), reviewer=reviewer,
                )
            if method == "POST" and action == "notes":
                return 200, self.review.add_note(mid, body.get("body", ""), reviewer)
            if method == "POST" and action == "review":
                return 200, self.review.set_review_state(mid, body.get("state"), reviewer)
            if method == "POST" and action == "confirm":
                return 200, self.review.confirm_match(mid, reviewer)
            if method == "POST" and action == "reassign":
                return 200, self.review.reassign(mid, sl_key=body.get("sl_key"), pl_key=body.get("pl_key"), reviewer=reviewer)
            if method == "POST" and action == "reject":
                return 200, self.review.reject_match(mid, body.get("reason", ""), reviewer)
        if method == "GET" and path == "/api/health":
            return 200, self.health()
        if method == "GET" and path == "/api/version":
            return 200, {"name": "cl-review-platform", "version": __version__}
        if method == "GET" and path == "/api/tools":
            return 200, {"items": self.service.tools()}
        if path == "/api/connections":
            if method == "GET":
                return 200, {"items": self.service.list_connections()}
            if method == "POST":
                return 200, self.service.add_connection(
                    body.get("role", ""), body.get("tool", ""), body.get("name", ""),
                    body.get("base_url", ""), body.get("config") or {}, body.get("auth_env") or {},
                )
        if path == "/api/mappings":
            if method == "GET":
                return 200, {"items": self.service.list_mappings()}
            if method == "POST":
                return 200, self.service.create_mapping(
                    body.get("name", ""), _int(body.get("pl_connection_id"), "pl_connection_id"),
                    body.get("pl_project", ""), body.get("pl_version"),
                    _int(body.get("sl_connection_id"), "sl_connection_id"), body.get("sl_scan", ""), reviewer,
                )
        m = re.fullmatch(r"/api/mappings/(\d+)", path)
        if method == "GET" and m:
            return 200, self.service.mapping(int(m.group(1)))
        if method == "POST" and path == "/api/deploy":
            from .deploy import deploy
            return 200, deploy(
                self.service,
                pl_connection_id=_int(body.get("pl_connection_id"), "pl_connection_id"),
                sl_connection_id=_int(body.get("sl_connection_id"), "sl_connection_id"),
                project_id=body.get("project_id", ""), version_id=body.get("version_id", ""),
                folder=body.get("folder"), parent_folder=body.get("parent_folder"),
                agents=body.get("agents"), dry_run=bool(body.get("dry_run", True)), actor=reviewer,
            )
        if method == "POST" and path == "/api/sync/preview":
            mapping_id = _int(body.get("mapping_id"), "mapping_id")
            ids = body.get("component_ids")
            matches = self.service._current_matches(mapping_id)
            if ids:
                matches = [x for x in matches if x["id"] in set(ids)]
            out = []
            for match in matches:
                decisions = self.db.query("SELECT * FROM decisions WHERE match_id=?", (match["id"],))
                from .sync import preview_match
                items = preview_match(self.service, match, decisions)
                if items:
                    out.append({"match": {"id": match["id"], "pl_name": match["pl_name"],
                                          "pl_version": match["pl_version"], "sl_name": match["sl_name"],
                                          "sl_version": match["sl_version"],
                                          "approved_for_sync": match.get("review_state") == "approved_for_sync"},
                                "items": items})
            return 200, {"mapping_id": mapping_id, "components": out}
        if path == "/api/sync/jobs":
            if method == "POST":
                return 200, self.sync.create_job(
                    _int(body.get("mapping_id"), "mapping_id"), dry_run=bool(body.get("dry_run", True)),
                    component_ids=body.get("component_ids"), confirm=bool(body.get("confirm")), actor=reviewer,
                )
            if method == "GET":
                mid = q("mapping_id")
                return 200, {"items": self.sync.list_jobs(int(mid) if mid else None, int(q("limit", "50") or 50))}
        m = re.fullmatch(r"/api/sync/jobs/(\d+)(/retry)?", path)
        if m:
            jid = int(m.group(1))
            if method == "GET" and not m.group(2):
                return 200, self.sync.job(jid)
            if method == "POST" and m.group(2):
                return 200, self.sync.retry(jid, confirm=bool(body.get("confirm")), actor=reviewer)
        if method == "GET" and path == "/api/audit":
            mid = q("mapping_id")
            return 200, {"items": self.service.audit_events(int(mid) if mid else None,
                                                            min(500, int(q("limit", "100") or 100)))}
        raise KeyError(f"no route for {method} {path}")


REVIEWER_REQUIRED = (
    "Reviewer name required. Type your name in the Reviewer box at the top right, then repeat "
    "the action: every decision and write is recorded against it."
)

# POSTs that only compute. They must keep working before anyone has named themselves.
READ_SHAPED_POSTS = frozenset({"/api/sync/preview"})


def _int(value: Any, name: str) -> int:
    """Required integer id from a JSON body: a bad or missing value is a client error."""
    if value is None or value == "":
        raise ValueError(f"{name} is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None


def _safe_reviewer(raw: str | None) -> str:
    """Self-declared reviewer name: any script and spaces are kept, control and markup
    characters are dropped. Never falls back to a placeholder name - an unattributed
    decision is worse than a refused one."""
    return re.sub(r"[\x00-\x1f\x7f\"'&<>\\]", "", (raw or "").strip())[:80]


class Handler(BaseHTTPRequestHandler):
    api: Api
    auth_token: str | None = None
    server_version = "clreview"
    sys_version = ""

    def version_string(self) -> str:
        return "clreview"

    def log_message(self, fmt: str, *args: Any) -> None:  # concise, no credentials
        logging.getLogger("clreview.http").info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not self.auth_token:
            return True
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(header, f"Bearer {self.auth_token}")

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from None

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if method == "GET" and parsed.path == "/api/health":
            self._send(200, self.api.health())
            return
        if not self._authorized():
            self._send(401, {"error": "authentication required"})
            return
        if parsed.path.startswith("/api/"):
            reviewer = _safe_reviewer(self.headers.get("X-Reviewer"))
            if method != "GET" and parsed.path not in READ_SHAPED_POSTS and not reviewer:
                self._send(400, {"error": REVIEWER_REQUIRED, "reviewer_required": True})
                return
            try:
                body = self._body() if method == "POST" else {}
                status, payload = self.api.handle(method, parsed.path, urllib.parse.parse_qs(parsed.query), body, reviewer)
                self._send(status, payload)
            except KeyError as exc:
                self._send(404, {"error": str(exc)})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except UnsupportedOperation as exc:
                self._send(409, {"error": str(exc), "unsupported": True})
            except ConnectorError as exc:
                self._send(502, {"error": str(exc), "status": exc.status, "unsupported": exc.unsupported})
            except Exception:  # never leak a token or a traceback to the client
                logging.getLogger("clreview.api").exception("unhandled API error")
                self._send(500, {"error": "internal server error"})
            return
        self._static(parsed.path)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_ROOT, rel))
        if not (full == WEB_ROOT or full.startswith(WEB_ROOT + os.sep)) or not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            return
        ext = os.path.splitext(full)[1]
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


def serve(db: DB, host: str = "127.0.0.1", port: int = 8765, fixtures_dir: str | None = None,
          auth_token: str | None = None) -> ThreadingHTTPServer:
    api = Api(db, fixtures_dir=fixtures_dir)
    handler = type("Handler", (Handler,), {"api": api, "auth_token": auth_token})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.api = api  # exposed for tests and embedding
    return httpd
