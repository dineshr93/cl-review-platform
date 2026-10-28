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


class LengthRequired(Exception):
    """A chunked body has no length to bound the read against, so it is not read at all."""


def _message(exc: BaseException) -> str:
    """`str(KeyError("x"))` renders the repr, quotes and all: a 404 body is not a traceback."""
    args = getattr(exc, "args", ())
    return str(args[0]) if args else str(exc)


# (method, path pattern, handler) - the whole HTTP surface in one place. Patterns are matched
# with ``fullmatch``, so the order carries no meaning and an unlisted path is a 404.
ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"/api/connections/(\d+)/test"), "_test_connection"),
    ("GET", re.compile(r"/api/connections/(\d+)/projects"), "_list_projects"),
    ("GET", re.compile(r"/api/connections/(\d+)/scans"), "_list_scans"),
    ("GET", re.compile(r"/api/connections/(\d+)/folders"), "_list_folders"),
    ("POST", re.compile(r"/api/mappings/(\d+)/refresh"), "_refresh"),
    ("GET", re.compile(r"/api/mappings/(\d+)/overview"), "_overview"),
    ("GET", re.compile(r"/api/mappings/(\d+)/components"), "_components"),
    ("POST", re.compile(r"/api/suggestions/(\d+)/decision"), "_decide_suggestion"),
    ("GET", re.compile(r"/api/components/(\d+)"), "_detail"),
    ("POST", re.compile(r"/api/components/(\d+)/decisions"), "_decide"),
    ("POST", re.compile(r"/api/components/(\d+)/notes"), "_add_note"),
    ("POST", re.compile(r"/api/components/(\d+)/review"), "_set_review_state"),
    ("POST", re.compile(r"/api/components/(\d+)/confirm"), "_confirm_match"),
    ("POST", re.compile(r"/api/components/(\d+)/reassign"), "_reassign"),
    ("POST", re.compile(r"/api/components/(\d+)/reject"), "_reject_match"),
    ("POST", re.compile(r"/api/components/(\d+)/suggestions"), "_add_suggestion"),
    ("GET", re.compile(r"/api/health"), "_health"),
    ("GET", re.compile(r"/api/version"), "_version"),
    ("GET", re.compile(r"/api/tools"), "_tools"),
    ("GET", re.compile(r"/api/connections"), "_list_connections"),
    ("POST", re.compile(r"/api/connections"), "_add_connection"),
    ("GET", re.compile(r"/api/mappings"), "_list_mappings"),
    ("POST", re.compile(r"/api/mappings"), "_create_mapping"),
    ("GET", re.compile(r"/api/mappings/(\d+)"), "_mapping"),
    ("POST", re.compile(r"/api/deploy"), "_deploy"),
    ("POST", re.compile(r"/api/sync/preview"), "_sync_preview"),
    ("POST", re.compile(r"/api/sync/jobs"), "_create_job"),
    ("GET", re.compile(r"/api/sync/jobs"), "_list_jobs"),
    ("GET", re.compile(r"/api/sync/jobs/(\d+)"), "_job"),
    ("POST", re.compile(r"/api/sync/jobs/(\d+)/retry"), "_retry_job"),
    ("GET", re.compile(r"/api/audit"), "_audit"),
)


class Api:
    def __init__(self, db: DB, fixtures_dir: str | None = None):
        self.db = db
        self.service = Service(db, fixtures_dir=fixtures_dir)
        self.review = ReviewService(self.service)
        self.sync = SyncService(self.service)

    # --- dispatch -----------------------------------------------------------
    # One table, one small method per route. Every pattern is anchored with ``fullmatch``, so a
    # route can never steal another's path (``/api/mappings/5/components`` cannot be taken by
    # ``/api/mappings/(\d+)``), and a path with no entry - ``GET /api/components/5/decisions``,
    # ``POST /api/components/5`` - falls through to the same 404 as the old if-chain.

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

        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            m = pattern.fullmatch(path)
            if m:
                return getattr(self, handler)(m, body, reviewer, q)
        raise KeyError(f"no route for {method} {path}")

    # --- route handlers -----------------------------------------------------
    def _test_connection(self, m, body, reviewer, q):
        return 200, self.service.test_connection(int(m.group(1)), reviewer)

    def _list_projects(self, m, body, reviewer, q):
        return 200, {"items": self.service.list_projects(int(m.group(1)))}

    def _list_scans(self, m, body, reviewer, q):
        return 200, {"items": self.service.list_scans(int(m.group(1)))}

    def _list_folders(self, m, body, reviewer, q):
        return 200, {"items": self.service.list_folders(int(m.group(1)))}

    def _refresh(self, m, body, reviewer, q):
        return 200, self.service.refresh(int(m.group(1)), reviewer)

    def _overview(self, m, body, reviewer, q):
        return 200, self.service.overview(int(m.group(1)))

    def _components(self, m, body, reviewer, q):
        return 200, self.review.list_components(
            int(m.group(1)), q=q("q"), review_status=q("review_status"), match_status=q("status"),
            confidence=q("confidence"), sort=q("sort", "name"),
            offset=int(q("offset", "0") or 0), limit=min(200, int(q("limit", "50") or 50)),
        )

    def _decide_suggestion(self, m, body, reviewer, q):
        return 200, self.review.decide_suggestion(int(m.group(1)), body.get("action", ""), reviewer,
                                                  note=body.get("note", ""))

    def _detail(self, m, body, reviewer, q):
        return 200, self.review.detail(int(m.group(1)))

    def _decide(self, m, body, reviewer, q):
        return 200, self.review.decide(
            int(m.group(1)), body.get("field", ""), body.get("entry_key", ""), body.get("action", ""),
            approved_value=body.get("approved_value"), note=body.get("note", ""), reviewer=reviewer,
        )

    def _add_note(self, m, body, reviewer, q):
        return 200, self.review.add_note(int(m.group(1)), body.get("body", ""), reviewer)

    def _set_review_state(self, m, body, reviewer, q):
        return 200, self.review.set_review_state(int(m.group(1)), body.get("state"), reviewer)

    def _confirm_match(self, m, body, reviewer, q):
        return 200, self.review.confirm_match(int(m.group(1)), reviewer)

    def _reassign(self, m, body, reviewer, q):
        return 200, self.review.reassign(int(m.group(1)), sl_key=body.get("sl_key"),
                                         pl_key=body.get("pl_key"), reviewer=reviewer)

    def _reject_match(self, m, body, reviewer, q):
        return 200, self.review.reject_match(int(m.group(1)), body.get("reason", ""), reviewer)

    def _add_suggestion(self, m, body, reviewer, q):
        return 200, self.review.add_suggestion(
            int(m.group(1)), body.get("field", ""), body.get("entry_key", ""), body.get("value", ""),
            confidence=body.get("confidence", 0.0), model=body.get("model", ""),
            evidence=body.get("evidence") or {},
        )

    def _health(self, m, body, reviewer, q):
        return 200, self.health()

    def _version(self, m, body, reviewer, q):
        return 200, {"name": "cl-review-platform", "version": __version__}

    def _tools(self, m, body, reviewer, q):
        return 200, {"items": self.service.tools()}

    def _list_connections(self, m, body, reviewer, q):
        return 200, {"items": self.service.list_connections()}

    def _add_connection(self, m, body, reviewer, q):
        return 200, self.service.add_connection(
            body.get("role", ""), body.get("tool", ""), body.get("name", ""),
            body.get("base_url", ""), body.get("config") or {}, body.get("auth_env") or {},
        )

    def _list_mappings(self, m, body, reviewer, q):
        return 200, {"items": self.service.list_mappings()}

    def _create_mapping(self, m, body, reviewer, q):
        return 200, self.service.create_mapping(
            body.get("name", ""), _int(body.get("pl_connection_id"), "pl_connection_id"),
            body.get("pl_project", ""), body.get("pl_version"),
            _int(body.get("sl_connection_id"), "sl_connection_id"), body.get("sl_scan", ""), reviewer,
        )

    def _mapping(self, m, body, reviewer, q):
        return 200, self.service.mapping(int(m.group(1)))

    def _deploy(self, m, body, reviewer, q):
        # Imported here so the subprocess module stays off the import path of every request.
        from .deploy import deploy

        return 200, deploy(
            self.service,
            pl_connection_id=_int(body.get("pl_connection_id"), "pl_connection_id"),
            sl_connection_id=_int(body.get("sl_connection_id"), "sl_connection_id"),
            project_id=body.get("project_id", ""), version_id=body.get("version_id", ""),
            folder=body.get("folder"), parent_folder=body.get("parent_folder"),
            agents=body.get("agents"), dry_run=bool(body.get("dry_run", True)), actor=reviewer,
        )

    def _sync_preview(self, m, body, reviewer, q):
        return 200, self.sync.preview(_int(body.get("mapping_id"), "mapping_id"), body.get("component_ids"))

    def _create_job(self, m, body, reviewer, q):
        return 200, self.sync.create_job(
            _int(body.get("mapping_id"), "mapping_id"), dry_run=bool(body.get("dry_run", True)),
            component_ids=body.get("component_ids"), confirm=bool(body.get("confirm")), actor=reviewer,
        )

    def _list_jobs(self, m, body, reviewer, q):
        mid = q("mapping_id")
        return 200, {"items": self.sync.list_jobs(int(mid) if mid else None, int(q("limit", "50") or 50))}

    def _job(self, m, body, reviewer, q):
        return 200, self.sync.job(int(m.group(1)))

    def _retry_job(self, m, body, reviewer, q):
        return 200, self.sync.retry(int(m.group(1)), confirm=bool(body.get("confirm")), actor=reviewer)

    def _audit(self, m, body, reviewer, q):
        mid = q("mapping_id")
        return 200, {"items": self.service.audit_events(int(mid) if mid else None,
                                                        min(500, int(q("limit", "100") or 100)))}


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
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # Without a length there is no bound to read against, and an unread body would
            # desynchronise the connection. HTTP/1.0 closes after the response, so nothing to drain.
            raise LengthRequired("Content-Length required: chunked request bodies are not supported")
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
            except LengthRequired as exc:
                self._send(411, {"error": str(exc)})
            except KeyError as exc:
                self._send(404, {"error": _message(exc)})
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
