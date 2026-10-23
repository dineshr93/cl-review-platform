"""Minimal HTTP client: timeouts, bounded retries, no credential logging."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BODY_IN_ERROR = 300


class HttpError(Exception):
    def __init__(self, status: int | None, message: str, body: str = "", url: str = ""):
        self.status = status
        self.body = body[:MAX_BODY_IN_ERROR]
        self.url = url
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "message": str(self), "body": self.body, "url": self.url}


class UnsupportedOperation(Exception):
    """Target tool exposes no documented API for this write."""


Transport = Callable[..., tuple[int, dict[str, str], bytes]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method.upper())
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:  # status-bearing failure, body kept for diagnostics
        return exc.code, dict(exc.headers or {}), exc.read() or b""
    except urllib.error.URLError as exc:
        raise HttpError(None, f"connection failed: {exc.reason}", url=url) from exc


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 30.0,
    retries: int = 2,
    transport: Transport | None = urllib_transport,
) -> tuple[int, dict[str, str], bytes]:
    """Perform one request with bounded exponential retry on transient statuses."""
    hdrs = {"Accept": "application/json", "User-Agent": "clreview/1.0"}
    hdrs.update(headers or {})
    payload: bytes | None = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    fn = transport or urllib_transport
    last: HttpError | None = None
    for attempt in range(retries + 1):
        try:
            status, rh, data = fn(method, url, hdrs, payload, timeout)
        except HttpError as exc:
            last = exc
            if attempt >= retries:
                raise
            time.sleep(0.05 * (2**attempt))
            continue
        if status in RETRY_STATUS and attempt < retries:
            time.sleep(0.05 * (2**attempt))
            continue
        return status, rh, data
    raise last or HttpError(None, "request failed", url=url)


def get_json(url: str, **kwargs: Any) -> Any:
    status, _, data = request("GET", url, **kwargs)
    if status >= 400:
        raise HttpError(status, f"GET {url} failed", data.decode("utf-8", "replace"), url)
    if not data:
        return None
    return json.loads(data.decode("utf-8"))


def get_text(url: str, **kwargs: Any) -> str:
    status, _, data = request("GET", url, **kwargs)
    if status >= 400:
        raise HttpError(status, f"GET {url} failed", data.decode("utf-8", "replace"), url)
    return data.decode("utf-8", "replace")


def put_json(url: str, body: Any, **kwargs: Any) -> tuple[int, Any]:
    status, _, data = request("PUT", url, body=body, **kwargs)
    text = data.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    return status, parsed


def join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def paginated(get_page: Callable[[int], tuple[list[dict[str, Any]], int | None]], limit: int = 100) -> list[dict[str, Any]]:
    """Fetch pages until a page returns fewer than `limit` or total is reached."""
    out: list[dict[str, Any]] = []
    page = 0
    while True:
        items, total = get_page(page)
        out.extend(items)
        page += 1
        if not items or len(items) < limit or (total is not None and len(out) >= total) or page > 200:
            return out
